"""Verify a wheel installation through real CLI tasks outside the source tree.

Run with the isolated environment's Python and -I. A JSON report contains every
command and its output; failed checks also preserve the temporary project.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
from importlib.metadata import version
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
import traceback


WORKLOAD = '''import hashlib
import sys
from pathlib import Path
import pyruns

cfg = pyruns.load()
run = pyruns.get_run_index()
package = Path(pyruns.__file__).resolve().parent
digest = hashlib.sha256()
for path in sorted(package.rglob("*")):
    if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
        digest.update(path.relative_to(package).as_posix().encode() + b"\\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
for step in range(cfg.steps):
    pyruns.track(step=step, score=run * 10000 + step)
pyruns.record(attempt=run, steps=cfg.steps, label=cfg.label,
              enabled=cfg.nested.enabled, values=list(cfg.nested["values"]),
              package_sha256=digest.hexdigest(), package_version=pyruns.__version__,
              python=sys.executable)
print(f"wheel-import:{package}", flush=True)
print(f"wheel-run-{run}", flush=True)
print(f"wheel-stderr-{run}", file=sys.stderr, flush=True)
raise SystemExit(42 if run == 1 else 0)
'''


def _owned_processes(project, identities):
    import psutil

    processes = {}
    for process in psutil.process_iter(["cmdline"]):
        try:
            command = process.info["cmdline"] or []
            if "pyruns.cli.detached_runner" not in command:
                continue
            workspace = Path(command[command.index("--workspace") + 1]).resolve()
            if project not in workspace.parents:
                continue
            for owned in [process, *process.children(recursive=True)]:
                processes[(owned.pid, owned.create_time())] = owned
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, IndexError):
            continue
    for pid, created in identities:
        try:
            process = psutil.Process(pid)
            if abs(process.create_time() - created) < 0.001 and process.status() != psutil.STATUS_ZOMBIE:
                processes[(pid, created)] = process
        except psutil.NoSuchProcess:
            pass
    return list(processes.values())


def _wait_for_exit(project, identities):
    deadline = time.monotonic() + 15
    while processes := _owned_processes(project, identities):
        if time.monotonic() >= deadline:
            raise AssertionError(f"test processes did not exit: {[p.pid for p in processes]}")
        time.sleep(0.1)


def _cleanup(project, identities):
    import psutil

    processes = _owned_processes(project, identities)
    for process in reversed(processes):
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(processes, timeout=5)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=5)
    return {"terminated": [p.pid for p in processes], "remaining": [p.pid for p in alive]}


def _verify(project, report, identities):
    import pyruns
    from pyruns.web.app import create_app

    installed = Path(pyruns.__file__).resolve()
    expected_package = Path(sysconfig.get_path("purelib")).resolve() / "pyruns"
    assert installed.parent == expected_package, f"not an isolated wheel import: {installed}"
    assert create_app().title == "Pyruns API"
    assert pyruns.__version__ == version("pyruns")
    report.update(installed=str(installed), version=version("pyruns"))
    # Tasks deliberately import a private package copy. Compare all file bytes,
    # excluding bytecode, rather than expecting its temporary path to match.
    digest = hashlib.sha256()
    for path in sorted(installed.parent.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            digest.update(path.relative_to(installed.parent).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    report["package_sha256"] = digest.hexdigest()
    scripts = Path(sysconfig.get_path("scripts"))
    env = {
        key: value for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME"} and not key.startswith("PYRUNS_")
    }
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYRUNS_UPDATE_STATE_DIR=str(project / ".update"))
    env["PATH"] = str(scripts) + os.pathsep + env.get("PATH", "")

    def cli(*arguments, expected=0, program="pyr"):
        executable = scripts / (program + (".exe" if os.name == "nt" else ""))
        command = [str(executable), *map(str, arguments)]
        entry = {"argv": command, "expected_exit": expected}
        report["commands"].append(entry)
        started = time.monotonic()
        try:
            result = subprocess.run(command, cwd=project, env=env, capture_output=True,
                                    encoding="utf-8", errors="replace", timeout=90)
        except subprocess.TimeoutExpired as error:
            entry.update(timeout=True, stdout=(error.stdout or b"").decode("utf-8", "replace"),
                         stderr=(error.stderr or b"").decode("utf-8", "replace"))
            raise
        finally:
            entry["seconds"] = time.monotonic() - started
        entry.update(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
        assert result.returncode == expected, f"{command}: exit {result.returncode}, expected {expected}"
        return result.stdout

    def show(workspace, task):
        detail = json.loads(cli("-w", workspace, "show", task, "--json"))
        metadata = json.loads((Path(detail["directory"]) / "task_info.json").read_text(encoding="utf-8"))
        for pid, created in zip(metadata["pids"], metadata["pid_create_times"], strict=True):
            if pid and created:
                identities.add((int(pid), float(created)))
        return detail

    for program in ("pyr", "pyruns"):
        assert cli("--version", program=program).strip() == f"{program} {report['version']}"
    cli("help")
    report["checks"].append("installed imports, app, and both entry points")

    # Exact argv must survive spaces, Unicode, and literal shell syntax.
    report["phase"] = "shell execution"
    arguments = ["two words", "literal;$(echo unexpected)", "中文"]
    payload = 'import json, sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))'
    cli("exec", "-d", "-n", "wheel-shell", "--", sys.executable, "-I", "-c", payload, *arguments)
    cli("-w", "shell", "wait", "wheel-shell", "--timeout", "60")
    shell = show("shell", "wheel-shell")
    assert shell["status"] == "completed" and shell["exit_codes"] == [0]
    assert json.dumps(arguments, ensure_ascii=False) in cli("-w", "shell", "log", "wheel-shell").splitlines()
    report["checks"].append("detached shell exact argv and logs")

    report["phase"] = "config snapshot and failed run"
    config = {"steps": 600, "label": "wheel 中文", "nested": {"enabled": True, "values": [1, 2, 3]}}
    config_path = project / "input.yaml"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    script = project / "train.py"
    script.write_text(WORKLOAD, encoding="utf-8")
    cli("init", script, "--config", config_path)
    cli("-w", "train", "add", config_path, "-n", "recovery")
    pending = show("train", "recovery")
    assert pending["config"] == config and pending["status"] == "pending"
    config_path.write_text('steps: 1\nlabel: changed-after-creation\n', encoding="utf-8")
    cli("-w", "train", "run", "recovery", "--detach")
    cli("-w", "train", "wait", "recovery", "--timeout", "60", expected=1)
    first = show("train", "recovery@1")
    assert first["status"] == "failed" and first["exit_codes"] == [42]
    assert (Path(first["directory"]) / "tracks.sqlite3").is_file(), "external metric storage was not exercised"
    report["checks"].append("immutable config snapshot, detached failure, and external metric storage")

    report["phase"] = "rerun and history"
    cli("-w", "train", "run", "recovery")
    second = show("train", "recovery@2")
    assert second["status"] == "completed" and second["run_index"] == 2
    assert second["exit_codes"] == [42, 0] and second["config"] == config
    expected_records = [
        {"attempt": run, "steps": 600, "label": config["label"], "enabled": True,
         "values": [1, 2, 3], "package_sha256": report["package_sha256"],
         "package_version": report["version"], "python": sys.executable}
        for run in (1, 2)
    ]
    expected_tracks = [
        {"step": list(range(600)), "score": [run * 10000 + step for step in range(600)]}
        for run in (1, 2)
    ]
    assert first["records"] == expected_records[:1] and first["tracks"] == expected_tracks[:1]
    assert second["records"] == expected_records and second["tracks"] == expected_tracks
    logs = []
    for run, status, code in ((1, "failed", 42), (2, "completed", 0)):
        historical = show("train", f"recovery@{run}")["selected_run"]
        assert historical["status"] == status and historical["exit_code"] == code
        assert historical["record"] == expected_records[run - 1]
        assert historical["track"] == expected_tracks[run - 1]
        log = cli("-w", "train", "log", f"recovery@{run}")
        assert f"wheel-run-{run}" in log.splitlines() and f"wheel-stderr-{run}" in log.splitlines()
        logs.append(log)
    report["checks"].append("successful rerun with exact independent history, records, curves, and logs")

    report["phase"] = "exports"
    exported = json.loads(cli("-w", "train", "export", "recovery", "-f", "json"))
    csv_rows = list(csv.DictReader(io.StringIO(cli("-w", "train", "export", "recovery", "-f", "csv"))))
    assert len(exported) == len(csv_rows) == 2
    for run, row, csv_row in zip((1, 2), exported, csv_rows, strict=True):
        assert {key: row[key] for key in expected_records[run - 1]} == expected_records[run - 1]
        expected_lifecycle = {"name": "recovery", "run": run,
                              "status": "failed" if run == 1 else "completed", "exit_code": 42 if run == 1 else 0}
        assert {key: row[key] for key in expected_lifecycle} == expected_lifecycle
        assert {key: csv_row[key] for key in expected_lifecycle} == {
            key: str(value) for key, value in expected_lifecycle.items()
        }
        assert {key: csv_row[key] for key in expected_records[run - 1]} == {
            key: str(value) for key, value in expected_records[run - 1].items()
        }
    report["checks"].append("CSV and JSON exports retain both run outcomes and records")

    report["phase"] = "rename, trash, and restore"
    cli("-w", "train", "mv", "recovery", "retained")
    cli("-w", "train", "show", "recovery", "--json", expected=1)
    cli("-w", "train", "rm", "retained")
    cli("-w", "train", "show", "retained", "--json", expected=1)
    cli("-w", "train", "restore", "retained")
    restored = show("train", "retained")
    for key in ("status", "config", "run_index", "exit_codes", "records", "tracks"):
        assert restored[key] == second[key], f"restore changed {key}"
    for run, log in enumerate(logs, 1):
        assert cli("-w", "train", "log", f"retained@{run}") == log
    report["tasks"] = [shell, restored]
    report["checks"].append("rename, trash, and restore preserve configuration and complete history")

    report["phase"] = "process exit"
    _wait_for_exit(project, identities)
    report["checks"].append("task processes and detached runners exited")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="JSON report path; diagnostics are saved beside it")
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"python": sys.executable, "passed": False, "commands": [], "checks": [], "phase": "imports"}
    identities = set()
    with tempfile.TemporaryDirectory(prefix="pyruns-installed-") as directory:
        project = Path(directory).resolve()
        report["project"] = str(project)
        try:
            _verify(project, report, identities)
            report["passed"] = True
        except Exception:
            report["error"] = traceback.format_exc()
        finally:
            try:
                report["cleanup"] = _cleanup(project, identities)
                if report["cleanup"]["terminated"]:
                    report["passed"] = False
            except Exception:
                report["passed"] = False
                report["cleanup_error"] = traceback.format_exc()
            if not report["passed"]:
                diagnostics = output.parent / project.name
                try:
                    shutil.copytree(project, diagnostics)
                    report["diagnostics"] = str(diagnostics)
                except OSError:
                    report["diagnostics_error"] = traceback.format_exc()
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in {"commands", "tasks"}},
                     ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
