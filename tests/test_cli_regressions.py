"""Regression tests for one-shot CLI automation contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyruns._config import CONFIG_DEFAULT_FILENAME, TASKS_DIR
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


def _submission_manager(tasks_dir: Path, tasks: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(tasks_dir=str(tasks_dir), tasks=tasks)


def _runner_process(exit_code: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(pid=4242, poll=lambda: exit_code)


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


def test_detached_runner_exits_when_claimed_task_state_disappears(tmp_path, monkeypatch):
    from pyruns.cli import detached_runner

    events = []
    task = {"name": "lost", "dir": str(tmp_path / "tasks" / "lost"), "status": "pending"}

    class FakeTaskManager:
        def __init__(self, **_kwargs):
            pass

        def get_task(self, name):
            return task if name == "lost" else None

        def start_task_now(self, name, execution_mode=None):
            return name == "lost" and execution_mode == "thread"

        def shutdown(self):
            events.append("shutdown")

    monkeypatch.setattr(
        detached_runner,
        "_parse_args",
        lambda: SimpleNamespace(
            workspace=str(tmp_path),
            backend="thread",
            jobs=1,
            submission_token="submission-token",
            tasks_json='["lost"]',
            startup_file=str(tmp_path / "startup.json"),
        ),
    )
    monkeypatch.setattr(detached_runner, "TaskManager", FakeTaskManager)
    monkeypatch.setattr(detached_runner, "load_task_info", lambda _task_dir: {})
    monkeypatch.setattr(
        detached_runner.time,
        "sleep",
        lambda _seconds: pytest.fail("runner should exit instead of polling a missing task forever"),
    )

    assert detached_runner.main() == 1
    assert events == ["shutdown"]


def test_cli_listing_does_not_take_over_or_fail_queued_tasks(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    tasks_dir = workspace / TASKS_DIR
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("queued-observer", {"value": 1})
    update_task_info(task["dir"], lambda info: info.update({"status": "queued"}))

    result = subprocess.run(
        _source_cli_command("-w", "shell", "ls"),
        cwd=tmp_path,
        env=_source_env(),
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "queued-observer" in result.stdout
    assert load_task_info(task["dir"])["status"] == "queued"


def test_batch_submission_accepts_queued_handshake_without_run_index(tmp_path, monkeypatch):
    from pyruns.cli import runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    generator = TaskGenerator(root_dir=str(tasks_dir))
    tasks = [
        generator.create_task("batch-a", {"value": 1}),
        generator.create_task("batch-b", {"value": 2}),
    ]

    def fake_popen(command, _env):
        assert command[command.index("--backend") + 1] == "thread"
        assert command[command.index("--jobs") + 1] == "1"
        submission_token = command[command.index("--submission-token") + 1]
        startup_file = Path(command[command.index("--startup-file") + 1])
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
        startup_file.write_text(json.dumps({"status": "ready"}), encoding="utf-8")
        return _runner_process()

    monkeypatch.setattr(runner, "_detached_popen", fake_popen)
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})

    assert runner.submit_cli_tasks(
        _submission_manager(tasks_dir, tasks),
        ["batch-a", "batch-b"],
        max_workers=1,
        startup_timeout=0.2,
    ) is True


def test_batch_submission_rejects_foreign_queued_handshake(tmp_path, monkeypatch):
    from pyruns.cli import runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("batch-a", {"value": 1})

    def fake_popen(command, _env):
        update_task_info(
            task["dir"],
            lambda info: info.update({"status": "queued", "runner_id": "host:9999:foreign"}),
        )
        startup_file = Path(command[command.index("--startup-file") + 1])
        startup_file.write_text(json.dumps({"status": "error"}), encoding="utf-8")
        return _runner_process()

    monkeypatch.setattr(runner, "_detached_popen", fake_popen)
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})
    killed: list[int] = []
    monkeypatch.setattr(runner, "kill_process", killed.append)

    assert runner.submit_cli_tasks(
        _submission_manager(tasks_dir, [task]),
        ["batch-a"],
        startup_timeout=0.1,
    ) is False
    assert killed == []


def test_submission_accepts_fast_failed_task_as_claimed(tmp_path, monkeypatch):
    from pyruns.cli import runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("fast-failure", {"value": 1})

    def fake_popen(command, _env):
        update_task_info(
            task["dir"],
            lambda info: info.update({"status": "failed", "run_index": 1}),
        )
        startup_file = Path(command[command.index("--startup-file") + 1])
        startup_file.write_text(json.dumps({"status": "ready"}), encoding="utf-8")
        return _runner_process(1)

    monkeypatch.setattr(runner, "_detached_popen", fake_popen)
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})

    assert runner.submit_cli_tasks(_submission_manager(tasks_dir, [task]), ["fast-failure"]) is True


def test_submission_timeout_does_not_kill_a_runner_after_partial_claim(tmp_path, monkeypatch):
    from pyruns.cli import runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    tasks = [
        TaskGenerator(root_dir=str(tasks_dir)).create_task("batch-a", {"value": 1}),
        TaskGenerator(root_dir=str(tasks_dir)).create_task("batch-b", {"value": 2}),
    ]

    def fake_popen(command, _env):
        token = command[command.index("--submission-token") + 1]
        update_task_info(
            tasks[0]["dir"],
            lambda info: info.update({"status": "queued", "runner_id": f"host:9999:{token}"}),
        )
        return _runner_process()

    monkeypatch.setattr(runner, "_detached_popen", fake_popen)
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})
    killed: list[int] = []
    monkeypatch.setattr(runner, "kill_process", killed.append)

    assert runner.submit_cli_tasks(
        _submission_manager(tasks_dir, tasks),
        ["batch-a", "batch-b"],
        startup_timeout=0.05,
    ) is True
    assert killed == []


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
