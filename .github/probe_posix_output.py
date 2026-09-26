"""Keep kernel bytes, adapter bytes, task logs and CLI output separate."""

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import platform
import pty
import select
import subprocess
import sys
import tempfile
from unittest.mock import patch


EXPRESSION = "printf 'alpha\\n'; printf 'beta\\n'"
COMMAND = ["/bin/sh", "-c", EXPRESSION]


def kernel_probe(hold_slave):
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            COMMAND, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave,
            start_new_session=True, close_fds=True,
        )
        if not hold_slave:
            os.close(slave)
            slave = -1
        status = process.wait(timeout=5)
        chunks = []
        while select.select([master], [], [], 0.1)[0]:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return {"status": status, "output": b"".join(chunks).decode()}
    finally:
        os.close(master)
        if slave >= 0:
            os.close(slave)


def adapter_probe():
    from pyruns.utils.terminal_capture import spawn_terminal_process

    process = spawn_terminal_process(COMMAND, cwd=None, env=os.environ)
    try:
        status = process.wait(timeout=5)
        output = b"".join(iter(lambda: process.stdout.read1(4096), b""))
        return {"status": status, "output": output.decode()}
    finally:
        process.close_output()


def cli_probe(wait_before_reader):
    from pyruns.cli import commands
    from pyruns.cli.app import main
    from pyruns.core import executor

    original_spawn = executor._spawn_captured_process

    def spawn(*args, **kwargs):
        process = original_spawn(*args, **kwargs)
        if wait_before_reader:
            process.wait(timeout=5)
        return process

    original_directory = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="pyruns-output-probe-") as directory:
        os.chdir(directory)
        captured = io.StringIO()
        try:
            with (
                patch.object(commands, "_find_project_root", return_value=None),
                patch.object(executor, "_spawn_captured_process", side_effect=spawn),
                contextlib.redirect_stdout(captured),
            ):
                status = main(["exec", "--name", "shell-expression", "-c", EXPRESSION])
            task = Path(directory, "_pyruns_", "_shell_", "tasks", "shell-expression")
            logs = {str(path.relative_to(task)): path.read_text(errors="replace")
                    for path in task.rglob("run*.log")}
            info = {str(path.relative_to(task)): path.read_text(errors="replace")
                    for path in task.glob("task_info.*")}
            return {"status": status, "output": captured.getvalue(),
                    "logs": logs, "task_info": info}
        finally:
            os.chdir(original_directory)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root))
    os.environ["PYRUNS_UPDATE_STATE_DIR"] = str(output.parent / "update")
    os.environ["PYRUNS_UI_SESSION_STATE_DIR"] = str(output.parent / "session")
    report = {
        "platform": platform.platform(), "python": sys.version,
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "cases": {},
    }
    cases = [
        ("kernel-close-slave", lambda: kernel_probe(False), 40),
        ("kernel-hold-slave", lambda: kernel_probe(True), 40),
        ("adapter-read-after-exit", adapter_probe, 40),
        ("cli-natural", lambda: cli_probe(False), 6),
        ("cli-read-after-exit", lambda: cli_probe(True), 6),
    ]
    for name, probe, rounds in cases:
        results = [probe() for _ in range(rounds)]
        report["cases"][name] = results
        summary = {"case": name, "rounds": rounds,
                   "missing": sum("alpha" not in item["output"] or "beta" not in item["output"] for item in results),
                   "log_missing": sum(bool(item.get("logs")) and not any("alpha" in text and "beta" in text for text in item["logs"].values()) for item in results)}
        print(json.dumps(summary), flush=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
