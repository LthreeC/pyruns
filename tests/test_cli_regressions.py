"""Regression tests for one-shot CLI automation contracts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pyruns._config import CONFIG_DEFAULT_FILENAME, ENV_KEY_CLI_SHELL_EXECUTABLE, TASKS_DIR
from pyruns.launcher import bootstrap_shell_workspace, bootstrap_workspace
from pyruns.core.task_generator import TaskGenerator
from pyruns.utils.info_io import load_task_info, update_task_info


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _source_cli_command(*args: str) -> list[str]:
    argv_json = json.dumps(["pyr", *args])
    code = (
        "import json,sys; "
        f"sys.argv=json.loads({argv_json!r}); "
        "from pyruns.cli import main; "
        "raise SystemExit(main())"
    )
    return [sys.executable, "-c", code]


def _source_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(PROJECT_ROOT) if not existing else os.pathsep.join([str(PROJECT_ROOT), existing])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _wait_for_status(task_dir: Path, statuses: set[str], timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = load_task_info(str(task_dir))
        if str(info.get("status", "")) in statuses:
            return info
        time.sleep(0.05)
    pytest.fail(f"Task did not reach {sorted(statuses)} within {timeout}s")


@pytest.mark.skipif(os.name != "nt", reason="Windows process flags only")
def test_detached_runner_never_allocates_a_console_window(monkeypatch):
    from pyruns.cli import runner

    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    runner._detached_popen([sys.executable, "-V"], os.environ.copy())

    flags = int(captured["creationflags"])
    assert flags & subprocess.CREATE_NO_WINDOW
    assert flags & subprocess.DETACHED_PROCESS
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL
    assert Path(captured["cwd"]).resolve() == PROJECT_ROOT


def test_detached_shell_run_returns_before_task_finishes(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))

    started = time.monotonic()
    result = subprocess.run(
        _source_cli_command(
            "exec",
            "--name",
            "detach-regression",
            "--detach",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(4); print('done')",
        ),
        cwd=tmp_path,
        env=_source_env(),
        capture_output=True,
        text=True,
        timeout=10,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stdout + result.stderr
    assert elapsed < 3.0

    task_dir = tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "detach-regression"
    info = _wait_for_status(task_dir, {"completed", "failed"})
    assert info["status"] == "completed"
    assert "done" in (task_dir / "run_logs" / "run1.log").read_text(encoding="utf-8")


def test_foreground_shell_failure_returns_nonzero(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    env = _source_env()
    if os.name == "nt":
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if powershell:
            env[ENV_KEY_CLI_SHELL_EXECUTABLE] = powershell

    result = subprocess.run(
        _source_cli_command(
            "exec",
            "--name",
            "failure-regression",
            "--",
            sys.executable,
            "-c",
            "import sys; sys.exit(7)",
        ),
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode != 0
    task_dir = tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "failure-regression"
    assert load_task_info(str(task_dir))["status"] == "failed"


def test_batch_submission_accepts_queued_handshake_without_run_index(tmp_path, monkeypatch):
    from pyruns.cli import runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    generator = TaskGenerator(root_dir=str(tasks_dir))
    tasks = [
        generator.create_task("batch-a", {"value": 1}),
        generator.create_task("batch-b", {"value": 2}),
    ]

    class DummyTaskManager:
        def __init__(self):
            self.tasks_dir = str(tasks_dir)
            self.tasks = tasks

    class FakeProcess:
        pid = 4242

        @staticmethod
        def poll():
            return None

    def fake_popen(command, _env):
        submission_token = command[command.index("--submission-token") + 1]
        for task in tasks:
            update_task_info(
                task["dir"],
                lambda info: info.update(
                    {
                        "status": "queued",
                        "runner_id": f"host:9999:{submission_token}",
                    }
                ),
            )
        return FakeProcess()

    monkeypatch.setattr(runner, "_detached_popen", fake_popen)
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})

    assert runner.submit_cli_tasks(
        DummyTaskManager(),
        ["batch-a", "batch-b"],
        max_workers=1,
        startup_timeout=0.2,
    ) is True


def test_batch_submission_rejects_foreign_queued_handshake(tmp_path, monkeypatch):
    from pyruns.cli import runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("batch-a", {"value": 1})

    class DummyTaskManager:
        def __init__(self):
            self.tasks_dir = str(tasks_dir)
            self.tasks = [task]

    class FakeProcess:
        pid = 4242

        @staticmethod
        def poll():
            return None

    def fake_popen(_command, _env):
        update_task_info(
            task["dir"],
            lambda info: info.update({"status": "queued", "runner_id": "host:9999:foreign"}),
        )
        return FakeProcess()

    monkeypatch.setattr(runner, "_detached_popen", fake_popen)
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})
    killed: list[int] = []
    monkeypatch.setattr(runner, "kill_process", killed.append)

    assert runner.submit_cli_tasks(
        DummyTaskManager(),
        ["batch-a"],
        startup_timeout=0.1,
    ) is False
    assert killed == [4242]


def test_submission_accepts_fast_failed_task_as_claimed(tmp_path, monkeypatch):
    from pyruns.cli import runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("fast-failure", {"value": 1})

    class DummyTaskManager:
        def __init__(self):
            self.tasks_dir = str(tasks_dir)
            self.tasks = [task]

    class FakeProcess:
        pid = 4242

        @staticmethod
        def poll():
            return 1

    def fake_popen(_command, _env):
        update_task_info(
            task["dir"],
            lambda info: info.update({"status": "failed", "run_index": 1}),
        )
        return FakeProcess()

    monkeypatch.setattr(runner, "_detached_popen", fake_popen)
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})

    assert runner.submit_cli_tasks(DummyTaskManager(), ["fast-failure"]) is True


def test_one_shot_run_bootstrap_preserves_existing_default(tmp_path):
    script = tmp_path / "train.py"
    script.write_text(
        "\n".join(
            [
                "import argparse",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--x', type=int, default=1)",
                "parser.parse_args()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    custom = tmp_path / "custom.yaml"
    custom.write_text("x: 99\n", encoding="utf-8")

    workspace = Path(bootstrap_workspace(str(script)))
    default_path = workspace / CONFIG_DEFAULT_FILENAME
    before = default_path.read_text(encoding="utf-8")

    bootstrap_workspace(str(script), str(custom), preserve_default=True)

    assert default_path.read_text(encoding="utf-8") == before
