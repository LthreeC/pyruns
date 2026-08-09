"""Regression tests for one-shot CLI automation contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyruns._config import CONFIG_DEFAULT_FILENAME, TASKS_DIR
from pyruns.cli.runner import SubmissionResult
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


def test_foreground_interrupt_cancels_only_the_tasks_just_submitted(tmp_path, monkeypatch):
    from pyruns.cli import commands

    task = {
        "name": "foreground",
        "dir": str(tmp_path / "foreground"),
        "status": "pending",
    }
    cancel_requests: list[str] = []
    waits: list[tuple[list[str], float]] = []

    class FakeManager:
        def request_task_cancel(self, name: str) -> bool:
            cancel_requests.append(name)
            return True

    monkeypatch.setattr(
        commands,
        "submit_cli_tasks",
        lambda *_args, **_kwargs: SubmissionResult("accepted", ("foreground",), ()),
    )
    monkeypatch.setattr(
        commands,
        "_follow_task",
        lambda _task, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    def fake_wait(tasks, *, timeout=0.0, require_started=False):
        assert require_started is False
        waits.append(([str(item["name"]) for item in tasks], float(timeout)))
        return [{"name": "foreground", "status": "cancelled"}]

    monkeypatch.setattr(commands, "_wait_for_task_records", fake_wait)

    with pytest.raises(KeyboardInterrupt):
        commands._submit_and_wait(
            SimpleNamespace(json_output=False, program="pyr"),
            FakeManager(),
            [task],
            mode="thread",
            workers=1,
            detach=False,
        )

    assert cancel_requests == ["foreground"]
    assert waits == [(["foreground"], commands._INTERRUPT_CANCEL_TIMEOUT_SEC)]


def test_detached_submission_never_installs_foreground_interrupt_cleanup(monkeypatch):
    from pyruns.cli import commands

    task = {"name": "detached", "dir": "unused", "status": "pending"}

    class FakeManager:
        def request_task_cancel(self, _name: str) -> bool:
            pytest.fail("detached submission must not own later cancellation")

    monkeypatch.setattr(
        commands,
        "submit_cli_tasks",
        lambda *_args, **_kwargs: SubmissionResult("accepted", ("detached",), ()),
    )
    monkeypatch.setattr(
        commands,
        "_task_record",
        lambda item: {"name": item["name"], "status": "queued"},
    )

    assert commands._submit_and_wait(
        SimpleNamespace(json_output=False, program="pyr"),
        FakeManager(),
        [task],
        mode="thread",
        workers=1,
        detach=True,
    ) == 0


def test_foreground_exec_sigint_stops_and_verifies_the_managed_process(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    args = [
        "exec",
        "--name",
        "foreground-sigint",
        "--",
        sys.executable,
        "-c",
        "import time; print('ready', flush=True); time.sleep(20)",
    ]
    code = (
        "import signal, threading\n"
        "threading.Timer(1.5, lambda: signal.raise_signal(signal.SIGINT)).start()\n"
        "from pyruns.cli.app import main\n"
        f"raise SystemExit(main({args!r}))\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=_source_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )

    stdout, stderr = process.communicate(timeout=25)

    assert process.returncode == 130, stdout + stderr
    assert "interrupted; stopping submitted task: foreground-sigint" in stderr
    task_dir = workspace / TASKS_DIR / "foreground-sigint"
    info = load_task_info(str(task_dir))
    assert info["status"] == "cancelled"
    assert info["run_statuses"][-1] == "cancelled"
    assert info["pids"][-1]
    assert info["pid_create_times"][-1]

    from pyruns.utils.process_utils import process_identity_matches

    assert not process_identity_matches(info["pids"][-1], info["pid_create_times"][-1])


def test_log_follow_sigint_stops_observing_without_stopping_the_task(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    submitted = subprocess.run(
        _source_cli_command(
            "exec",
            "--name",
            "observe-sigint",
            "--detach",
            "--",
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(20)",
        ),
        cwd=tmp_path,
        env=_source_env(),
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert submitted.returncode == 0, submitted.stdout + submitted.stderr

    task_dir = workspace / TASKS_DIR / "observe-sigint"
    deadline = time.monotonic() + 10
    info = load_task_info(str(task_dir))
    while info.get("status") != "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        info = load_task_info(str(task_dir))
    assert info["status"] == "running"

    args = ["-w", "shell", "log", "observe-sigint", "--follow"]
    code = (
        "import signal, threading\n"
        "threading.Timer(0.5, lambda: signal.raise_signal(signal.SIGINT)).start()\n"
        "from pyruns.cli.app import main\n"
        f"raise SystemExit(main({args!r}))\n"
    )
    observer = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=_source_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        stdout, stderr = observer.communicate(timeout=10)
        assert observer.returncode == 130, stdout + stderr
        assert "stopping submitted task" not in stderr
        active = load_task_info(str(task_dir))
        assert active["status"] == "running"

        from pyruns.utils.process_utils import process_identity_matches

        assert process_identity_matches(
            active["pids"][-1],
            active["pid_create_times"][-1],
        )
    finally:
        stopped = subprocess.run(
            _source_cli_command(
                "-w",
                "shell",
                "stop",
                "observe-sigint",
                "--timeout",
                "10",
            ),
            cwd=tmp_path,
            env=_source_env(),
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr


def test_stop_verifies_the_entire_managed_process_tree_has_exited(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    child_identity_file = tmp_path / "child-identity.json"
    parent_code = (
        "import json,pathlib,psutil,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); "
        f"pathlib.Path({str(child_identity_file)!r}).write_text("
        "json.dumps({'pid': child.pid, 'created_at': psutil.Process(child.pid).create_time()}),"
        "encoding='utf-8'); "
        "time.sleep(20)"
    )
    submitted = subprocess.run(
        _source_cli_command(
            "exec",
            "--name",
            "process-tree",
            "--detach",
            "--",
            sys.executable,
            "-c",
            parent_code,
        ),
        cwd=tmp_path,
        env=_source_env(),
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert submitted.returncode == 0, submitted.stdout + submitted.stderr

    task_dir = workspace / TASKS_DIR / "process-tree"
    deadline = time.monotonic() + 10
    while not child_identity_file.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert child_identity_file.is_file()
    child_identity = json.loads(child_identity_file.read_text(encoding="utf-8"))
    active = load_task_info(str(task_dir))
    assert active["status"] == "running"

    stopped = subprocess.run(
        _source_cli_command(
            "-w",
            "shell",
            "stop",
            "process-tree",
            "--timeout",
            "10",
        ),
        cwd=tmp_path,
        env=_source_env(),
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr

    final = load_task_info(str(task_dir))
    assert final["status"] == "cancelled"
    assert final["run_statuses"][-1] == "cancelled"

    from pyruns.utils.process_utils import process_identity_matches

    assert not process_identity_matches(final["pids"][-1], final["pid_create_times"][-1])
    assert not process_identity_matches(
        child_identity["pid"],
        child_identity["created_at"],
    )


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


def test_dev_server_subprocess_uses_hidden_window_flags(monkeypatch):
    from pyruns.cli import commands
    from pyruns.utils.process_utils import hidden_subprocess_kwargs

    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(commands, "bootstrap_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(commands.subprocess, "run", fake_run)
    args = SimpleNamespace(
        script="train.py",
        config=None,
        port=None,
        no_browser=True,
        browser=False,
    )

    assert commands.cmd_dev(None, args) == 0
    assert captured["command"][:3] == [sys.executable, "-m", "pyruns.web.app"]
    assert captured["check"] is False
    for key, value in hidden_subprocess_kwargs().items():
        assert captured[key] == value


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

    result = runner.submit_cli_tasks(
        _submission_manager(tasks_dir, tasks),
        ["batch-a", "batch-b"],
        max_workers=1,
        startup_timeout=0.2,
    )
    assert result == SubmissionResult("accepted", ("batch-a", "batch-b"), ())


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

    result = runner.submit_cli_tasks(
        _submission_manager(tasks_dir, [task]),
        ["batch-a"],
        startup_timeout=0.1,
    )
    assert result == SubmissionResult("rejected", (), ("batch-a",))
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

    result = runner.submit_cli_tasks(_submission_manager(tasks_dir, [task]), ["fast-failure"])
    assert result == SubmissionResult("accepted", ("fast-failure",), ())


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

    result = runner.submit_cli_tasks(
        _submission_manager(tasks_dir, tasks),
        ["batch-a", "batch-b"],
        startup_timeout=0.05,
    )
    assert result == SubmissionResult("partial", ("batch-a",), ("batch-b",))
    assert killed == []


def test_partial_submission_is_reported_and_worker_count_is_bounded(tmp_path, monkeypatch, capsys):
    from pyruns.cli import commands

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    tasks = [
        TaskGenerator(root_dir=str(tasks_dir)).create_task("batch-a", {"value": 1}),
        TaskGenerator(root_dir=str(tasks_dir)).create_task("batch-b", {"value": 2}),
    ]
    captured: dict[str, int] = {}

    def submit(_manager, _names, *, max_workers, **_kwargs):
        captured["max_workers"] = max_workers
        return SubmissionResult("partial", ("batch-a",), ("batch-b",))

    monkeypatch.setattr(commands, "submit_cli_tasks", submit)

    result = commands._submit_and_wait(
        SimpleNamespace(json_output=False, program="pyr"),
        _submission_manager(tasks_dir, tasks),
        tasks,
        mode="thread",
        workers=999999,
        detach=True,
    )

    assert result == 1
    assert captured["max_workers"] == 2
    error = capsys.readouterr().err
    assert "submission partial" in error
    assert "Claimed: batch-a" in error
    assert "Unclaimed: batch-b" in error


def test_log_writer_keeps_consuming_after_stdout_pipe_closes(tmp_path, monkeypatch):
    from pyruns.cli import commands

    log_path = tmp_path / "run1.log"
    log_path.write_text("task failed after consumer exited\n", encoding="utf-8")

    class ClosedPipe:
        encoding = "utf-8"

        def write(self, _content):
            raise BrokenPipeError

        def flush(self):
            raise BrokenPipeError

        def fileno(self):
            raise OSError

    monkeypatch.setattr(commands.sys, "stdout", ClosedPipe())

    offset = commands._write_available_log(str(log_path), 0)

    assert offset == log_path.stat().st_size


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
