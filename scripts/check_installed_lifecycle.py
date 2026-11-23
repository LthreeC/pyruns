"""Verify an installation through real CLI tasks and UI outside the source tree.

Run with the isolated environment's Python and -I. A JSON report contains every
command and its output; failed checks also preserve the temporary project.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from importlib.metadata import version
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
import traceback
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener


WORKLOAD = '''import faulthandler
import hashlib
import os
import sys
from pathlib import Path
import pyruns

faulthandler.dump_traceback_later(30, repeat=True)
cfg = pyruns.load()
run = pyruns.get_run_index()
package = Path(pyruns.__file__).resolve().parent
digest = hashlib.sha256()
for path in sorted(package.rglob("*")):
    if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
        digest.update(path.relative_to(package).as_posix().encode() + b"\\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
for step in range(cfg.steps):
    if step % 100 == 0:
        pyruns.track_many([{"step": step, "score": run * 10000 + step}])
    else:
        pyruns.track(step=step, score=run * 10000 + step)
    if step % 100 == 0:
        print(f"wheel-step-{run}:{step}", flush=True)
pyruns.record(attempt=run, steps=cfg.steps, label=cfg.label,
              enabled=cfg.nested.enabled, values=list(cfg.nested["values"]),
              run_attempt_id=os.environ.get("PYRUNS_RUN_ATTEMPT_ID"),
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


class _AssetRefs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = set()

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in {"src", "href"} and value and (value.startswith("/assets/") or value == "/pyruns.svg"):
                self.paths.add(value)


def _verify_ui(project, report, scripts, env, identities, package):
    import psutil
    from websockets.sync.client import connect
    from websockets.typing import Origin

    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    executable = scripts / ("pyr.exe" if os.name == "nt" else "pyr")
    command = [str(executable), "ui", "shell", "--no-browser", "--port", str(port)]
    entry: dict[str, Any] = {"argv": command}
    report["ui"] = entry
    log_path = project / "ui-server.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=project, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            identities.add((process.pid, psutil.Process(process.pid).create_time()))
            cookies = CookieJar()
            opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(cookies))
            deadline = time.monotonic() + 30
            while True:
                assert process.poll() is None, "UI server exited before accepting requests"
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                token_url = next((line.removeprefix("[pyruns] UI: ") for line in lines
                                  if line.startswith(f"[pyruns] UI: {base_url}/?token=")), None)
                if token_url:
                    try:
                        with opener.open(token_url, timeout=1) as response:
                            html = response.read()
                            assert response.status == 200 and response.geturl() == base_url + "/"
                        break
                    except HTTPError:
                        raise
                    except (URLError, OSError):
                        pass  # The CLI prints the URL just before the server binds.
                if time.monotonic() >= deadline:
                    raise AssertionError("UI server did not become ready within 30 seconds")
                time.sleep(0.1)

            static = package / "web" / "static"
            assert html == (static / "index.html").read_bytes(), "UI did not serve the installed frontend"
            parser = _AssetRefs()
            parser.feed(html.decode("utf-8"))
            assert any(path.endswith(".js") for path in parser.paths)
            assert any(path.endswith(".css") for path in parser.paths)
            assert "/pyruns.svg" in parser.paths
            for path in sorted(parser.paths):
                with opener.open(base_url + path, timeout=10) as response:
                    assert response.status == 200
                    assert response.read() == (static / path.removeprefix("/")).read_bytes(), path
            entry["assets"] = sorted(parser.paths)

            with opener.open(base_url + "/api/workspace", timeout=10) as response:
                workspace = json.load(response)
            assert workspace["workspace_kind"] == "shell" and workspace["workspace_ready"]
            assert project in Path(workspace["run_root"]).resolve().parents, workspace
            entry["workspace"] = workspace

            notes = "installed UI notes 中文\nsecond line"
            request = Request(base_url + "/api/tasks/wheel-shell/notes", method="PATCH",
                              data=json.dumps({"notes": notes, "expected_notes": ""}).encode("utf-8"),
                              headers={"Content-Type": "application/json", "Origin": base_url})
            with opener.open(request, timeout=10) as response:
                updated = json.load(response)
            assert updated["ok"] and updated["task"]["notes"] == notes, updated
            entry["notes_roundtrip"] = True

            with opener.open(
                base_url + "/api/tasks/wheel-shell/runs/2/compare?other_task=wheel-shell&other_run=1",
                timeout=10,
            ) as response:
                comparison = json.load(response)
            assert comparison["schema_version"] == 1, comparison
            assert comparison["left"]["reference"] == "wheel-shell@1", comparison
            assert comparison["right"]["reference"] == "wheel-shell@2", comparison
            for side in ("left", "right"):
                run = comparison[side]
                assert run["status"] == "completed" and run["exit_code"] == 0, run
                assert run["input_available"] is False and run["duration_seconds"] >= 0, run
                assert "environment" in run and "source_state" in run and "record" in run, run
                assert "tracks" not in run, run
            assert comparison["input_diff"]["available"] is False, comparison
            assert comparison["input_diff"]["equal"] is None, comparison
            entry["run_comparison"] = comparison

            cookie_header = "; ".join(f"{cookie.name}={cookie.value}" for cookie in cookies)
            log_query = urlencode({"log_file_name": "run2.log", "tail_lines": 0})
            with opener.open(base_url + "/api/tasks/wheel-shell/logs?" + log_query, timeout=10) as response:
                log_cursor = json.load(response)
            assert log_cursor["content"] == "" and log_cursor["offset"] > 0, log_cursor
            log_selection = {
                "log_file_name": "run2.log", "log_identity": log_cursor["log_identity"],
                "expected_workspace": workspace["run_root"],
            }
            download_url = base_url + "/api/tasks/wheel-shell/logs/download?" + urlencode(log_selection)
            with opener.open(download_url, timeout=10) as response:
                downloaded = response.read()
                assert len(downloaded) == int(response.headers["Content-Length"]) == log_cursor["offset"]
                assert response.headers["X-Pyruns-Log-Identity"] == log_cursor["log_identity"]
                assert response.headers["Accept-Ranges"] == "none"
                assert "wheel-shell--run2.log" in response.headers["Content-Disposition"]
            with opener.open(
                base_url + "/api/tasks/wheel-shell/logs?" + urlencode({"log_file_name": "run2.log", "offset": 0}),
                timeout=10,
            ) as response:
                log_content = json.load(response)
            assert downloaded.decode("utf-8", errors="replace") == log_content["content"]
            log_stream_query = urlencode({**log_selection, "offset": log_cursor["offset"]})
            with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
                with connect(f"ws://127.0.0.1:{port}/api/tasks/wheel-shell/logs/stream?{log_stream_query}",
                             sock=connection, origin=Origin(base_url), additional_headers={"Cookie": cookie_header},
                             open_timeout=10, close_timeout=5) as logs:
                    ready = json.loads(logs.recv(timeout=10))
                    assert ready == {"type": "ready", "task_name": "wheel-shell"}, ready
            entry["log_download"] = {"bytes": len(downloaded), "identity": log_cursor["log_identity"]}
            report["checks"].append("installed complete log download, zero-line cursor, and log stream readiness")
            # A supplied socket bypasses proxy auto-detection across websockets 12-15.
            with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
                with connect(f"ws://127.0.0.1:{port}/api/tasks/events", sock=connection,
                             origin=Origin(base_url), additional_headers={"Cookie": cookie_header},
                             open_timeout=10, close_timeout=5) as events:
                    ready = json.loads(events.recv(timeout=10))
                    assert ready == {"type": "ready", "revision": 0}, ready
                    entry["event"] = ready
            report["checks"].append("installed UI server, authentication, frontend assets, API read/write, and WebSocket")
            report["checks"].append("installed historical run comparison without curve expansion")
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise AssertionError("UI server did not stop within 15 seconds") from None
            finally:
                entry["exit_code"] = process.returncode
                entry["log"] = log_path.read_text(encoding="utf-8", errors="replace")


def _verify(project, report, identities):
    report["dependencies"] = {name: version(name) for name in (
        "omegaconf", "PyYAML", "fastapi", "pydantic", "starlette", "anyio", "uvicorn", "websockets",
    )}
    import pyruns
    from pyruns.web.app import create_app

    installed = Path(pyruns.__file__).resolve()
    expected_package = Path(sysconfig.get_path("purelib")).resolve() / "pyruns"
    assert installed.parent == expected_package, f"not an isolated installation: {installed}"
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

    def cli(*arguments, expected=0, program="pyr", input_text=None):
        executable = scripts / (program + (".exe" if os.name == "nt" else ""))
        command = [str(executable), *map(str, arguments)]
        entry = {"argv": command, "expected_exit": expected}
        report["commands"].append(entry)
        started = time.monotonic()
        try:
            result = subprocess.run(command, cwd=project, env=env, input=input_text, capture_output=True,
                                    encoding="utf-8", errors="replace", timeout=90,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode("utf-8", "replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
            stderr = error.stderr.decode("utf-8", "replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
            entry.update(timeout=True, stdout=stdout, stderr=stderr)
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
    diagnostics = json.loads(cli("doctor", "--json"))
    assert diagnostics["schema_version"] == 1 and diagnostics["ok"], diagnostics
    report["checks"].append("installed imports, app, and both entry points")
    report["checks"].append("workspace-free installation diagnostics")

    # Saved stdin scripts must survive spaces, Unicode, and literal shell syntax.
    # -I ignores PYTHONUTF8, so select UTF-8 explicitly for Windows pipe output.
    report["phase"] = "shell execution"
    arguments = ["two words", "literal;$(echo unexpected)", "中文"]
    payload = 'import json, sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))'
    if os.name == "nt":
        # Automation may have no terminal ancestor and otherwise follow cmd.exe.
        # Select the interpreter matching the PowerShell script below explicitly.
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        assert powershell, "PowerShell is required for the Windows installation check"
        cli("init")
        cli("config", "set", "shell_executable", powershell)
        cli("config", "set", "shell_mode", "custom")
    parts = [sys.executable, "-I", "-X", "utf8", "-c", payload, *arguments]
    expression = (
        "& " + " ".join("'" + part.replace("'", "''") + "'" for part in parts)
        if os.name == "nt" else shlex.join(parts)
    )
    script_text = "# Saved command and explicit parameters\n" + expression + "\n"
    cli("exec", "-d", "-n", "wheel-shell", "--stdin", input_text=script_text)
    cli("-w", "shell", "wait", "wheel-shell", "--timeout", "60")
    shell = show("shell", "wheel-shell")
    assert shell["status"] == "completed" and shell["exit_codes"] == [0]
    assert Path(shell["payload"]).read_text(encoding="utf-8") == script_text
    assert shell["command_mode"] == "shell" and shell["command_argv"] is None
    shell_raw_log = cli("-w", "shell", "log", "wheel-shell")
    shell_log = re.sub(r"\x1b\[[0-9;]*m", "", shell_raw_log)
    assert json.dumps(arguments, ensure_ascii=False) in shell_log.splitlines()
    shell_tail = cli("-w", "shell", "log", "wheel-shell", "--tail", "1")
    assert shell_tail and shell_raw_log.endswith(shell_tail)
    assert len(shell_tail.rstrip("\n").split("\n")) == 1
    assert cli("-w", "shell", "log", "wheel-shell", "--tail", "0") == ""
    Path(shell["payload"]).write_text(script_text + "echo wheel-shell-rerun\n", encoding="utf-8", newline="\n")
    cli("-w", "shell", "run", "wheel-shell")
    shell = show("shell", "wheel-shell")
    assert shell["exit_codes"] == [0, 0]
    assert "wheel-shell-rerun" in cli("-w", "shell", "log", "wheel-shell@2").splitlines()
    assert all(show("shell", f"wheel-shell@{run}")["selected_run"]["input"] is None for run in (1, 2))
    assert not list((Path(shell["directory"]) / "run_logs").glob("run*.input.json"))
    difference = json.loads(cli("-w", "shell", "diff", "wheel-shell@1", "wheel-shell@2", "--json", expected=1))
    assert difference["available"] is False
    report["checks"].append("detached stdin script, saved parameters, logs, and rerun from the edited shell file")

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
    import yaml

    assert yaml.safe_load(first["selected_run"]["input"]["payload_text"]) == config
    assert (Path(first["directory"]) / "tracks.sqlite3").is_file(), "external metric storage was not exercised"
    report["checks"].append("immutable config snapshot, detached failure, and external metric storage")

    report["phase"] = "rerun and history"
    cli("-w", "train", "run", "recovery")
    second = show("train", "recovery@2")
    assert second["status"] == "completed" and second["run_index"] == 2
    assert second["exit_codes"] == [42, 0] and second["config"] == config
    assert json.loads(cli("-w", "train", "diff", "recovery@1", "recovery@2", "--json"))["equal"]
    metadata = json.loads((Path(second["directory"]) / "task_info.json").read_text(encoding="utf-8"))
    attempt_ids = metadata["run_attempt_ids"]
    assert len(attempt_ids) == 2 and len(set(attempt_ids)) == 2, attempt_ids
    assert all(isinstance(token, str) and re.fullmatch(r"[0-9a-f]{32}", token) for token in attempt_ids)
    expected_records = [
        {"attempt": run, "steps": 600, "label": config["label"], "enabled": True,
         "values": [1, 2, 3], "package_sha256": report["package_sha256"],
         "run_attempt_id": attempt_ids[run - 1],
         "package_version": report["version"], "python": sys.executable}
        for run in (1, 2)
    ]
    expected_tracks = [
        {"step": list(range(600)), "score": [run * 10000 + step for step in range(600)]}
        for run in (1, 2)
    ]
    assert first["records"] == expected_records[:1] and first["tracks"] == expected_tracks[:1]
    assert second["records"] == expected_records and second["tracks"] == expected_tracks
    report["checks"].append("distinct run identities reach SDK processes and match persistent history")
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

    # Exercise the packaged dependency in the isolated installation, including
    # a historical run that is no longer the default terminal log.
    from threading import Event
    from pyruns.utils.log_search import LogSearch
    search = LogSearch().search(second["directory"], "wheel-run-1", Event())
    assert not search["errors"] and search["match_count"] == 1
    assert search["matches"][0]["log_file"] == "run1.log"
    report["checks"].append("installed ripgrep dependency finds historical log with byte context")

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

    report["phase"] = "UI server"
    _verify_ui(project, report, scripts, env, identities, installed.parent)

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
