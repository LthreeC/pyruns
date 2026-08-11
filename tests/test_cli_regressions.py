"""Regression tests for one-shot CLI automation contracts."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyruns._config import CONFIG_DEFAULT_FILENAME, TASKS_DIR
from pyruns.cli.runner import SubmissionResult
from pyruns.cli.submission_protocol import (
    SCHEMA_VERSION,
    atomic_write_json,
    read_submission_payload,
    read_submission_receipt,
    submission_control_paths,
    submission_payload_path,
    write_abort_request,
    write_submission_payload,
    write_submission_receipt,
)
from pyruns.launcher import bootstrap_shell_workspace, bootstrap_workspace
from pyruns.core.task_generator import TaskGenerator
from pyruns.utils.info_io import ensure_run_slot, load_task_info, update_task_info


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


def _submission_from_command(
    command: list[str],
) -> tuple[str, list[str], list[int], Path, Path]:
    token, receipt, abort = _submission_control_from_command(command)
    assert "--submissions-json" not in command
    workspace = Path(command[command.index("--workspace") + 1])
    payload_path = submission_payload_path(str(workspace / TASKS_DIR), token)
    payload = read_submission_payload(payload_path, token=token)
    names = list(payload.names)
    run_indices = list(payload.run_indices)
    return token, names, run_indices, receipt, abort


def _submission_control_from_command(command: list[str]) -> tuple[str, Path, Path]:
    token = command[command.index("--submission-token") + 1]
    workspace = Path(command[command.index("--workspace") + 1])
    receipt, abort = submission_control_paths(str(workspace / TASKS_DIR), token)
    return token, Path(receipt), Path(abort)


def _write_test_submission(
    workspace: Path,
    token: str,
    names: list[str],
    run_indices: list[int],
) -> Path:
    payload_path = Path(submission_payload_path(str(workspace / TASKS_DIR), token))
    write_submission_payload(
        str(payload_path),
        token=token,
        names=names,
        run_indices=run_indices,
    )
    return payload_path


def _write_test_receipt(
    command: list[str],
    status: str,
    claimed: list[str],
    *,
    runner_pid: int = 4242,
) -> tuple[Path, Path]:
    token, names, run_indices, receipt, abort = _submission_from_command(command)
    write_submission_receipt(
        str(receipt),
        token=token,
        runner_pid=runner_pid,
        status=status,
        names=names,
        run_indices=run_indices,
        claimed=claimed,
    )
    return receipt, abort


def test_foreground_interrupt_cancels_only_the_tasks_just_submitted(tmp_path, monkeypatch):
    from pyruns.cli import commands

    task = {
        "name": "foreground",
        "dir": str(tmp_path / "foreground"),
        "status": "pending",
    }
    cancel_requests: list[tuple[str, str | None, int | None]] = []
    waits: list[tuple[list[str], float]] = []
    runner_id = "host:4242:foreground-token"

    class FakeManager:
        def request_task_cancel(
            self,
            name: str,
            *,
            expected_runner_id: str | None = None,
            expected_run_index: int | None = None,
        ) -> bool:
            cancel_requests.append((name, expected_runner_id, expected_run_index))
            return True

    monkeypatch.setattr(
        commands,
        "submit_cli_tasks",
        lambda *_args, **_kwargs: SubmissionResult(
            "accepted",
            ("foreground",),
            (),
            runner_id=runner_id,
        ),
    )
    monkeypatch.setattr(
        commands,
        "_follow_task",
        lambda _task, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    def fake_wait(tasks, identities, *, timeout=0.0, require_started=False):
        assert require_started is False
        assert identities == {
            "foreground": commands._TaskRunIdentity(
                run_index=1,
                runner_id=runner_id,
                started_queued=True,
            )
        }
        waits.append(([str(item["name"]) for item in tasks], float(timeout)))
        return [{"name": "foreground", "status": "cancelled"}]

    monkeypatch.setattr(commands, "_wait_for_task_records", fake_wait)

    with pytest.raises(KeyboardInterrupt):
        commands._submit_and_wait(
            SimpleNamespace(json_output=False, program="pyr"),
            FakeManager(),
            [task],
            workers=1,
            detach=False,
        )

    assert cancel_requests == [("foreground", runner_id, 1)]
    assert waits == [(["foreground"], commands._INTERRUPT_CANCEL_TIMEOUT_SEC)]


def test_interrupt_cleanup_does_not_cancel_a_later_same_name_run(tmp_path, monkeypatch):
    from unittest.mock import patch

    from pyruns.cli import commands
    from pyruns.core.task_manager import TaskManager

    tasks_dir = tmp_path / TASKS_DIR
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("reused", {"value": 1})
    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    old_runner = "host:100:old-token"
    new_runner = "host:200:new-token"

    def start_newer_run(info):
        info.update(
            {
                "status": "running",
                "run_index": 2,
                "runner_id": new_runner,
                "run_statuses": ["completed", "running"],
            }
        )

    update_task_info(task["dir"], start_newer_run)
    monkeypatch.setattr(commands, "_eprint", lambda _message="": None)

    try:
        commands._cancel_submitted_tasks_after_interrupt(
            SimpleNamespace(program="pyr"),
            manager,
            [task],
            {
                "reused": commands._TaskRunIdentity(
                    run_index=1,
                    runner_id=old_runner,
                    started_queued=True,
                )
            },
        )
    finally:
        manager.shutdown()

    info = load_task_info(task["dir"])
    assert info["status"] == "running"
    assert info["run_index"] == 2
    assert info["runner_id"] == new_runner
    assert info.get("cancel_requested_at") is None


def test_wait_returns_the_captured_run_when_a_newer_run_starts(monkeypatch):
    from pyruns.cli import commands

    old_runner = "host:100:old-token"
    snapshots = iter(
        [
            {
                "name": "race",
                "status": "running",
                "run_index": 1,
                "runner_id": old_runner,
                "run_statuses": ["running"],
            },
            {
                "name": "race",
                "status": "running",
                "run_index": 2,
                "runner_id": "host:200:new-token",
                "run_statuses": ["completed", "running"],
            },
        ]
    )
    task = {"name": "race", "dir": "unused"}
    identity = commands._TaskRunIdentity(run_index=1, runner_id=old_runner)

    monkeypatch.setattr(commands, "load_task_info", lambda _task_dir: next(snapshots))
    monkeypatch.setattr(
        commands,
        "_task_record",
        lambda _task, *, info_snapshot=None, **_kwargs: {
            "name": "race",
            "status": info_snapshot["status"],
            "run_index": info_snapshot["run_index"],
            "pid": None,
            "latest_log": None,
        },
    )
    monkeypatch.setattr(
        commands,
        "_resolve_log_reference",
        lambda _task, *, run_index=None, **_kwargs: commands._LogReference(
            f"run{run_index}.log",
            run_index,
            "run",
        ),
    )
    monkeypatch.setattr(commands.time, "sleep", lambda _seconds: None)

    records = commands._wait_for_task_records(
        [task],
        {"race": identity},
    )

    assert records[0]["name"] == "race"
    assert records[0]["status"] == "completed"
    assert records[0]["run_index"] == 1


def test_log_follow_never_switches_to_a_newer_run_log(monkeypatch):
    from pyruns.cli import commands

    old_runner = "host:100:old-token"
    snapshots = iter(
        [
            {
                "name": "race",
                "status": "running",
                "run_index": 1,
                "runner_id": old_runner,
                "run_statuses": ["running"],
            },
            {
                "name": "race",
                "status": "running",
                "run_index": 2,
                "runner_id": "host:200:new-token",
                "run_statuses": ["completed", "running"],
            },
        ]
    )
    task = {"name": "race", "dir": "unused"}
    identity = commands._TaskRunIdentity(run_index=1, runner_id=old_runner)
    reads: list[str] = []

    monkeypatch.setattr(commands, "load_task_info", lambda _task_dir: next(snapshots))
    monkeypatch.setattr(
        commands,
        "_task_record",
        lambda _task, *, info_snapshot=None, **_kwargs: {
            "name": "race",
            "status": info_snapshot["status"],
            "run_index": info_snapshot["run_index"],
            "pid": None,
            "latest_log": None,
        },
    )

    def resolve_reference(_task, *, run_index=None, **_kwargs):
        assert run_index == 1
        return commands._LogReference("run1.log", 1, "run")

    monkeypatch.setattr(commands, "_resolve_log_reference", resolve_reference)
    monkeypatch.setattr(commands.os.path, "isfile", lambda path: path == "run1.log")
    monkeypatch.setattr(
        commands,
        "_write_available_log",
        lambda path, offset: reads.append(path) or offset,
    )
    monkeypatch.setattr(commands.time, "sleep", lambda _seconds: None)

    record = commands._follow_task(task, identity=identity)

    assert record["status"] == "completed"
    assert record["run_index"] == 1
    assert reads
    assert set(reads) == {"run1.log"}


def test_stop_rejects_a_run_identity_change_before_cancellation(monkeypatch):
    from pyruns.cli import commands

    task = {"name": "race", "dir": "unused", "status": "running"}
    identity = commands._TaskRunIdentity(
        run_index=1,
        runner_id="host:100:old-token",
    )

    class FakeManager:
        def request_task_cancel(
            self,
            name,
            *,
            expected_runner_id=None,
            expected_run_index=None,
        ):
            assert name == "race"
            assert expected_runner_id == identity.runner_id
            assert expected_run_index == identity.run_index
            return False

    monkeypatch.setattr(commands, "_resolve_exact_tasks", lambda _manager, _names: [task])
    monkeypatch.setattr(commands, "_capture_task_run_identity", lambda _task: identity)
    monkeypatch.setattr(
        commands,
        "_bound_task_record",
        lambda _task, _identity: {"name": "race", "status": "running"},
    )
    monkeypatch.setattr(
        commands,
        "_wait_for_task_records",
        lambda *_args, **_kwargs: pytest.fail("stop must not wait after an identity conflict"),
    )

    with pytest.raises(commands.CliError, match="run changed before cancellation"):
        commands.cmd_stop(
            SimpleNamespace(json_output=False),
            SimpleNamespace(tasks=["race"], timeout=1.0),
            FakeManager(),
        )


def test_stop_reports_partial_cancellation_when_one_run_identity_changes(
    monkeypatch,
    capsys,
):
    from pyruns.cli import commands

    tasks = [
        {"name": "first", "dir": "first-dir", "status": "running"},
        {"name": "second", "dir": "second-dir", "status": "running"},
    ]
    identities = {
        name: commands._TaskRunIdentity(
            run_index=1,
            runner_id=f"host:100:{name}",
        )
        for name in ("first", "second")
    }
    requests = []

    class FakeManager:
        def request_task_cancel(
            self,
            name,
            *,
            expected_runner_id=None,
            expected_run_index=None,
        ):
            requests.append((name, expected_runner_id, expected_run_index))
            return name == "first"

    monkeypatch.setattr(commands, "_resolve_exact_tasks", lambda _manager, _names: tasks)
    monkeypatch.setattr(
        commands,
        "_capture_task_run_identity",
        lambda task: identities[task["name"]],
    )
    monkeypatch.setattr(
        commands,
        "_bound_task_record",
        lambda task, _identity: {"name": task["name"], "status": "running"},
    )
    monkeypatch.setattr(
        commands,
        "_wait_for_task_records",
        lambda selected, _identities, **_kwargs: [
            {"name": task["name"], "status": "cancelled"}
            for task in selected
        ],
    )

    result = commands.cmd_stop(
        SimpleNamespace(json_output=True),
        SimpleNamespace(tasks=["first", "second"], timeout=1.0),
        FakeManager(),
    )

    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["stopped"] == [{"name": "first", "status": "cancelled"}]
    assert payload["not_stopped"] == ["second"]
    assert requests == [
        ("first", identities["first"].runner_id, 1),
        ("second", identities["second"].runner_id, 1),
    ]


def test_detached_submission_never_installs_foreground_interrupt_cleanup(monkeypatch):
    from pyruns.cli import commands

    task = {"name": "detached", "dir": "unused", "status": "pending"}

    class FakeManager:
        def request_task_cancel(self, _name: str) -> bool:
            pytest.fail("detached submission must not own later cancellation")

    monkeypatch.setattr(
        commands,
        "submit_cli_tasks",
        lambda *_args, **_kwargs: SubmissionResult(
            "accepted",
            ("detached",),
            (),
            runner_id="host:4242:detached-token",
        ),
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

    class FakeProcess:
        pid = 123

        def wait(self, timeout=None):
            captured["wait_timeout"] = timeout
            return 0

        def poll(self):
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(commands, "bootstrap_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(commands.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(commands, "get_process_create_time", lambda _pid: 1.0)
    args = SimpleNamespace(
        script="train.py",
        config=None,
        port=None,
        no_browser=True,
        browser=False,
    )

    assert commands.cmd_dev(None, args) == 0
    assert captured["command"][:3] == [sys.executable, "-m", "pyruns.web.app"]
    assert captured["wait_timeout"] is None
    for key, value in hidden_subprocess_kwargs().items():
        assert captured[key] == value


def test_dev_server_start_failure_is_a_cli_error(monkeypatch):
    from pyruns.cli import commands

    def fail_to_start(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(commands, "bootstrap_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(commands.subprocess, "Popen", fail_to_start)
    args = SimpleNamespace(
        script="train.py",
        config=None,
        port=None,
        no_browser=True,
        browser=False,
    )

    with pytest.raises(commands.CliError, match="could not start dev server: denied"):
        commands.cmd_dev(None, args)


def test_dev_server_interrupt_stops_and_reaps_process_tree(monkeypatch):
    from pyruns.cli import commands

    captured = {}

    class InterruptedProcess:
        pid = 456

        def wait(self, timeout=None):
            if timeout is None:
                raise KeyboardInterrupt
            captured["reap_timeout"] = timeout
            return -1

        def poll(self):
            return None

    monkeypatch.setattr(commands, "bootstrap_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(commands.subprocess, "Popen", lambda *_args, **_kwargs: InterruptedProcess())
    monkeypatch.setattr(commands, "get_process_create_time", lambda _pid: 12.5)

    def fake_kill(pid, expected_create_time=None, **_kwargs):
        captured["kill"] = (pid, expected_create_time)
        return True

    monkeypatch.setattr(commands, "kill_process", fake_kill)
    original_sigint_handler = commands.signal.getsignal(commands.signal.SIGINT)
    args = SimpleNamespace(
        script="train.py",
        config=None,
        port=None,
        no_browser=True,
        browser=False,
    )

    with pytest.raises(KeyboardInterrupt):
        commands.cmd_dev(None, args)

    assert captured["kill"] == (456, 12.5)
    assert captured["reap_timeout"] == 5
    assert commands.signal.getsignal(commands.signal.SIGINT) == original_sigint_handler


def test_detached_runner_exits_when_claimed_task_state_disappears(tmp_path, monkeypatch):
    from pyruns.cli import detached_runner

    events = []
    task = {"name": "lost", "dir": str(tmp_path / "tasks" / "lost"), "status": "pending"}
    token = "1" * 32
    payload_path = _write_test_submission(tmp_path, token, ["lost"], [1])

    class FakeTaskManager:
        def __init__(self, **_kwargs):
            pass

        def get_task(self, name):
            return task if name == "lost" else None

        def start_batch_tasks(
            self,
            names,
            max_workers=None,
            expected_run_indices=None,
        ):
            assert max_workers == 1
            assert expected_run_indices == {"lost": 1}
            return ["lost"] if names == ["lost"] else []

        def shutdown(self):
            events.append("shutdown")

    monkeypatch.setattr(
        detached_runner,
        "_parse_args",
        lambda: SimpleNamespace(
            workspace=str(tmp_path),
            jobs=1,
            submission_token=token,
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
    assert not payload_path.exists()
    receipt_path, _ = submission_control_paths(str(tmp_path / TASKS_DIR), token)
    receipt = read_submission_receipt(
        receipt_path,
        token=token,
        names=["lost"],
        run_indices=[1],
    )
    assert receipt is not None
    assert receipt.status == "accepted"


def test_detached_runner_observes_submitted_run_after_new_rerun_starts(
    tmp_path,
    monkeypatch,
):
    from pyruns.cli import detached_runner

    token = "f" * 32
    task = {
        "name": "repeat",
        "dir": str(tmp_path / TASKS_DIR / "repeat"),
        "status": "pending",
    }
    events: list[str] = []
    payload_path = _write_test_submission(tmp_path, token, ["repeat"], [1])

    class FakeTaskManager:
        def __init__(self, **_kwargs):
            pass

        def get_task(self, name):
            return task if name == "repeat" else None

        def start_batch_tasks(
            self,
            names,
            max_workers=None,
            expected_run_indices=None,
        ):
            assert names == ["repeat"]
            assert max_workers == 1
            assert expected_run_indices == {"repeat": 1}
            return ["repeat"]

        def shutdown(self):
            events.append("shutdown")

    monkeypatch.setattr(
        detached_runner,
        "_parse_args",
        lambda: SimpleNamespace(
            workspace=str(tmp_path),
            jobs=1,
            submission_token=token,
        ),
    )
    monkeypatch.setattr(detached_runner, "TaskManager", FakeTaskManager)
    monkeypatch.setattr(
        detached_runner,
        "load_task_info",
        lambda _task_dir: {
            "status": "running",
            "run_index": 2,
            "run_statuses": ["completed", "running"],
        },
    )
    monkeypatch.setattr(
        detached_runner.time,
        "sleep",
        lambda _seconds: pytest.fail("runner must not follow a newer rerun"),
    )

    assert detached_runner.main() == 0
    assert events == ["shutdown"]
    assert not payload_path.exists()

    receipt_path, _ = submission_control_paths(str(tmp_path / TASKS_DIR), token)
    receipt = read_submission_receipt(
        receipt_path,
        token=token,
        names=["repeat"],
        run_indices=[1],
    )
    assert receipt is not None
    assert receipt.status == "accepted"
    assert receipt.run_indices == (1,)


def test_detached_runner_rejects_run_completed_before_claim(tmp_path, monkeypatch):
    from pyruns.cli import detached_runner

    token = "e" * 32
    tasks_dir = tmp_path / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("race", {"value": 1})
    events: list[str] = []
    payload_path = _write_test_submission(tmp_path, token, ["race"], [1])

    def complete_first_run(info):
        slot = ensure_run_slot(info, 1)
        info["status"] = "completed"
        info["start_times"][slot] = "2026-08-10_10-00-00"
        info["finish_times"][slot] = "2026-08-10_10-00-01"
        info["run_statuses"][slot] = "completed"
        info["exit_codes"][slot] = 0

    class FakeTaskManager:
        def __init__(self, **_kwargs):
            pass

        def get_task(self, name):
            return task if name == "race" else None

        def start_batch_tasks(
            self,
            names,
            max_workers=None,
            expected_run_indices=None,
        ):
            assert names == ["race"]
            assert max_workers == 1
            assert expected_run_indices == {"race": 1}
            update_task_info(task["dir"], complete_first_run)
            events.append("claim-rejected")
            return []

        def shutdown(self):
            events.append("shutdown")

    monkeypatch.setattr(
        detached_runner,
        "_parse_args",
        lambda: SimpleNamespace(
            workspace=str(tmp_path),
            jobs=1,
            submission_token=token,
        ),
    )
    monkeypatch.setattr(detached_runner, "TaskManager", FakeTaskManager)

    assert detached_runner.main() == 2
    assert events == ["claim-rejected", "shutdown"]
    assert not payload_path.exists()

    info = load_task_info(task["dir"])
    assert info["status"] == "completed"
    assert info["run_index"] == 1
    assert info["run_statuses"] == ["completed"]

    receipt_path, _ = submission_control_paths(str(tasks_dir), token)
    receipt = read_submission_receipt(
        receipt_path,
        token=token,
        names=["race"],
        run_indices=[1],
    )
    assert receipt is not None
    assert receipt.status == "rejected"
    assert receipt.claimed == ()
    assert receipt.run_indices == (1,)


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
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "queued-observer" in result.stdout
    assert load_task_info(task["dir"])["status"] == "queued"


def test_batch_submission_accepts_exact_runner_receipt(tmp_path, monkeypatch):
    from pyruns.cli import runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    generator = TaskGenerator(root_dir=str(tasks_dir))
    tasks = [
        generator.create_task("batch-a", {"value": 1}),
        generator.create_task("batch-b", {"value": 2}),
    ]
    captured: dict[str, str] = {}

    def fake_popen(command, _env):
        assert "--backend" not in command
        assert command[command.index("--jobs") + 1] == "1"
        assert "--startup-file" not in command
        captured["token"] = _submission_from_command(command)[0]
        _write_test_receipt(command, "accepted", ["batch-a", "batch-b"])
        return _runner_process()

    monkeypatch.setattr(runner, "_detached_popen", fake_popen)
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})

    result = runner.submit_cli_tasks(
        _submission_manager(tasks_dir, tasks),
        ["batch-a", "batch-b"],
        expected_runs={"batch-a": 1, "batch-b": 1},
        max_workers=1,
        startup_timeout=0.2,
    )
    assert result.status == "accepted"
    assert result.claimed == ("batch-a", "batch-b")
    assert result.unclaimed == ()
    assert result.runner_id is not None
    host, pid, token = result.runner_id.rsplit(":", 2)
    assert host
    assert pid == "4242"
    assert token == captured["token"]


def test_large_batch_submission_keeps_runner_command_below_windows_limit(
    tmp_path,
    monkeypatch,
):
    from pyruns.cli import runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    names = [f"sweep_{index:04d}-of-0720" for index in range(1, 721)]
    run_indices = list(range(1, len(names) + 1))
    tasks = [{"name": name} for name in names]
    captured: dict[str, object] = {}

    def fake_popen(command, _env):
        token, submitted_names, submitted_runs, _receipt, _abort = (
            _submission_from_command(command)
        )
        captured["command"] = command
        captured["token"] = token
        assert submitted_names == names
        assert submitted_runs == run_indices
        _write_test_receipt(command, "accepted", names)
        return _runner_process()

    monkeypatch.setattr(runner, "_detached_popen", fake_popen)
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})

    result = runner.submit_cli_tasks(
        _submission_manager(tasks_dir, tasks),
        names,
        expected_runs=dict(zip(names, run_indices)),
        max_workers=8,
        startup_timeout=0.2,
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--submissions-json" not in command
    assert len(subprocess.list2cmdline(command)) < 32_767
    assert result.status == "accepted"
    payload_path = submission_payload_path(str(tasks_dir), str(captured["token"]))
    assert not Path(payload_path).exists()


def test_batch_submission_returns_exact_runner_rejection(tmp_path, monkeypatch):
    from pyruns.cli import runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("batch-a", {"value": 1})

    def fake_popen(command, _env):
        _write_test_receipt(command, "rejected", [])
        return _runner_process()

    monkeypatch.setattr(runner, "_detached_popen", fake_popen)
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})

    result = runner.submit_cli_tasks(
        _submission_manager(tasks_dir, [task]),
        ["batch-a"],
        expected_runs={"batch-a": 1},
        startup_timeout=0.1,
    )
    assert result == SubmissionResult("rejected", (), ("batch-a",))


def test_submission_removes_payload_when_runner_process_cannot_start(
    tmp_path,
    monkeypatch,
):
    from pyruns.cli import runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("launch-failure", {})
    captured: dict[str, Path] = {}

    def failed_popen(command, _env):
        token, names, run_indices, _receipt, _abort = _submission_from_command(command)
        assert names == ["launch-failure"]
        assert run_indices == [1]
        captured["payload"] = Path(submission_payload_path(str(tasks_dir), token))
        raise OSError("test process creation failure")

    monkeypatch.setattr(runner, "_detached_popen", failed_popen)
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})

    result = runner.submit_cli_tasks(
        _submission_manager(tasks_dir, [task]),
        ["launch-failure"],
        expected_runs={"launch-failure": 1},
    )

    assert result == SubmissionResult("rejected", (), ("launch-failure",))
    assert not captured["payload"].exists()


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
        _write_test_receipt(command, "accepted", ["fast-failure"])
        return _runner_process(1)

    monkeypatch.setattr(runner, "_detached_popen", fake_popen)
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})

    result = runner.submit_cli_tasks(
        _submission_manager(tasks_dir, [task]),
        ["fast-failure"],
        expected_runs={"fast-failure": 1},
    )
    assert result.status == "accepted"
    assert result.claimed == ("fast-failure",)
    assert result.unclaimed == ()
    assert result.runner_id is not None


def test_submission_uses_receipt_pid_when_windows_launcher_redirects(
    tmp_path,
    monkeypatch,
):
    from pyruns.cli import runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("redirected", {"value": 1})

    def fake_popen(command, _env):
        _write_test_receipt(
            command,
            "accepted",
            ["redirected"],
            runner_pid=5252,
        )
        return _runner_process(0)

    monkeypatch.setattr(runner, "_detached_popen", fake_popen)
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})

    result = runner.submit_cli_tasks(
        _submission_manager(tasks_dir, [task]),
        ["redirected"],
        expected_runs={"redirected": 1},
    )

    assert result.status == "accepted"
    assert result.runner_id is not None
    _host, pid, _token = result.runner_id.rsplit(":", 2)
    assert pid == "5252"


def test_submission_waits_for_redirected_runner_after_intermediate_receipt(
    tmp_path,
    monkeypatch,
):
    from pyruns.cli import runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("redirected", {"value": 1})
    captured: dict[str, list[str]] = {}

    def fake_popen(command, _env):
        captured["command"] = command
        _write_test_receipt(command, "starting", [], runner_pid=5252)
        return _runner_process(0)

    def publish_acceptance(_seconds):
        _write_test_receipt(
            captured["command"],
            "accepted",
            ["redirected"],
            runner_pid=5252,
        )

    monkeypatch.setattr(runner, "_detached_popen", fake_popen)
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})
    monkeypatch.setattr(runner.time, "sleep", publish_acceptance)

    result = runner.submit_cli_tasks(
        _submission_manager(tasks_dir, [task]),
        ["redirected"],
        expected_runs={"redirected": 1},
        startup_timeout=1.0,
    )

    assert result.status == "accepted"
    assert result.runner_id is not None
    _host, pid, _token = result.runner_id.rsplit(":", 2)
    assert pid == "5252"


def test_submission_waits_for_delayed_terminal_receipt_after_fast_runner_exit(
    tmp_path,
    monkeypatch,
):
    from pyruns.cli import runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("fast-exit", {"value": 1})
    original_read_receipt = runner._read_receipt
    read_count = 0

    def fake_popen(command, _env):
        _write_test_receipt(command, "accepted", ["fast-exit"])
        return _runner_process(0)

    def delayed_receipt(*args, **kwargs):
        nonlocal read_count
        read_count += 1
        if read_count <= 5:
            return None
        return original_read_receipt(*args, **kwargs)

    monkeypatch.setattr(runner, "_detached_popen", fake_popen)
    monkeypatch.setattr(runner, "_read_receipt", delayed_receipt)
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})

    result = runner.submit_cli_tasks(
        _submission_manager(tasks_dir, [task]),
        ["fast-exit"],
        expected_runs={"fast-exit": 1},
    )

    assert read_count == 6
    assert result.status == "accepted"
    assert result.claimed == ("fast-exit",)
    assert result.unclaimed == ()
    assert result.runner_id is not None


def test_foreign_terminal_state_cannot_claim_a_submission(tmp_path, monkeypatch):
    from pyruns.cli import runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("foreign", {"value": 1})
    paths: dict[str, Path] = {}

    def fake_popen(command, _env):
        update_task_info(
            task["dir"],
            lambda info: info.update({"status": "failed", "run_index": 1}),
        )
        token, _, _, receipt, abort = _submission_from_command(command)
        workspace = Path(command[command.index("--workspace") + 1])
        payload = Path(submission_payload_path(str(workspace / TASKS_DIR), token))
        paths.update(payload=payload, receipt=receipt, abort=abort)
        return _runner_process()

    monkeypatch.setattr(runner, "_detached_popen", fake_popen)
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})
    monkeypatch.setattr(runner, "get_process_create_time", lambda _pid: 10.0)
    monkeypatch.setattr(runner, "DEFAULT_ABORT_TIMEOUT_SEC", 0.01)
    kill_calls: list[tuple[int, float | None]] = []

    def cannot_kill(pid, *, expected_create_time=None):
        kill_calls.append((pid, expected_create_time))
        return False

    monkeypatch.setattr(runner, "kill_process", cannot_kill)

    result = runner.submit_cli_tasks(
        _submission_manager(tasks_dir, [task]),
        ["foreign"],
        expected_runs={"foreign": 1},
        startup_timeout=0.05,
    )
    assert result == SubmissionResult("unresolved", (), ("foreign",))
    assert kill_calls == [(4242, 10.0)]
    assert paths["payload"].is_file()
    assert paths["abort"].is_file()
    assert not paths["receipt"].is_file()


def test_abort_after_accepted_runner_exit_is_unresolved_and_keeps_claims(tmp_path):
    from pyruns.cli import runner

    token = "8" * 32
    names = ["batch-a", "batch-b"]
    receipt_file, abort_file = submission_control_paths(str(tmp_path), token)
    write_submission_receipt(
        receipt_file,
        token=token,
        runner_pid=4242,
        status="accepted",
        names=names,
        run_indices=[1, 1],
        claimed=names,
    )

    outcome = runner._abort_submission(
        _runner_process(1),
        process_create_time=10.0,
        receipt_file=receipt_file,
        abort_file=abort_file,
        token=token,
        names=names,
        run_indices=[1, 1],
        reason="test runner exit",
    )

    assert outcome.result == SubmissionResult("unresolved", tuple(names), ())


def test_abort_force_kill_after_partial_claim_keeps_unresolved_ownership(tmp_path, monkeypatch):
    from pyruns.cli import runner

    token = "9" * 32
    names = ["batch-a", "batch-b"]
    receipt_file, abort_file = submission_control_paths(str(tmp_path), token)
    write_submission_receipt(
        receipt_file,
        token=token,
        runner_pid=4242,
        status="claiming",
        names=names,
        run_indices=[1, 1],
        claimed=["batch-a"],
    )
    monkeypatch.setattr(runner, "DEFAULT_ABORT_TIMEOUT_SEC", 0.001)
    monkeypatch.setattr(runner, "kill_process", lambda *_args, **_kwargs: True)

    outcome = runner._abort_submission(
        _runner_process(),
        process_create_time=10.0,
        receipt_file=receipt_file,
        abort_file=abort_file,
        token=token,
        names=names,
        run_indices=[1, 1],
        reason="test timeout",
    )

    assert outcome.result == SubmissionResult("unresolved", ("batch-a",), ("batch-b",))


def test_abort_targets_receipt_runner_after_windows_launcher_exits(tmp_path, monkeypatch):
    from pyruns.cli import runner

    token = "d" * 32
    names = ["batch-a"]
    receipt_file, abort_file = submission_control_paths(str(tmp_path), token)
    write_submission_receipt(
        receipt_file,
        token=token,
        runner_pid=5252,
        status="claiming",
        names=names,
        run_indices=[1],
        claimed=[],
    )
    monkeypatch.setattr(runner, "DEFAULT_ABORT_TIMEOUT_SEC", 0.001)
    monkeypatch.setattr(
        runner,
        "get_process_create_time",
        lambda pid: 20.0 if pid == 5252 else None,
    )
    killed: list[tuple[int, float | None]] = []

    def record_kill(pid, *, expected_create_time=None):
        killed.append((pid, expected_create_time))
        return True

    monkeypatch.setattr(runner, "kill_process", record_kill)

    outcome = runner._abort_submission(
        _runner_process(0),
        process_create_time=10.0,
        receipt_file=receipt_file,
        abort_file=abort_file,
        token=token,
        names=names,
        run_indices=[1],
        reason="test redirected runner cleanup",
    )

    assert outcome.result == SubmissionResult("unresolved", (), ("batch-a",))
    assert outcome.runner_pid == 5252
    assert killed == [(5252, 20.0)]


def test_abort_force_kill_without_valid_receipt_remains_unresolved(tmp_path, monkeypatch):
    from pyruns.cli import runner

    token = "a" * 32
    names = ["batch-a"]
    receipt_file, abort_file = submission_control_paths(str(tmp_path), token)
    monkeypatch.setattr(runner, "DEFAULT_ABORT_TIMEOUT_SEC", 0.001)
    monkeypatch.setattr(runner, "kill_process", lambda *_args, **_kwargs: True)

    outcome = runner._abort_submission(
        _runner_process(),
        process_create_time=10.0,
        receipt_file=receipt_file,
        abort_file=abort_file,
        token=token,
        names=names,
        run_indices=[1],
        reason="test timeout",
    )

    assert outcome.result == SubmissionResult("unresolved", (), ("batch-a",))


def test_abort_force_kill_uses_owned_popen_when_create_time_is_unavailable(
    tmp_path,
    monkeypatch,
):
    from pyruns.cli import runner

    token = "c" * 32
    names = ["batch-a"]
    receipt_file, abort_file = submission_control_paths(str(tmp_path), token)
    monkeypatch.setattr(runner, "DEFAULT_ABORT_TIMEOUT_SEC", 0.001)
    kill_calls = []
    monkeypatch.setattr(
        runner,
        "kill_process",
        lambda pid, *, expected_create_time=None: kill_calls.append(
            (pid, expected_create_time)
        )
        or True,
    )

    outcome = runner._abort_submission(
        _runner_process(),
        process_create_time=None,
        receipt_file=receipt_file,
        abort_file=abort_file,
        token=token,
        names=names,
        run_indices=[1],
        reason="test missing process identity",
    )

    assert outcome.result == SubmissionResult("unresolved", (), ("batch-a",))
    assert kill_calls == [(4242, None)]


def test_abort_force_kill_with_observed_empty_claim_state_remains_unresolved(tmp_path, monkeypatch):
    from pyruns.cli import runner

    token = "b" * 32
    names = ["batch-a"]
    receipt_file, abort_file = submission_control_paths(str(tmp_path), token)
    write_submission_receipt(
        receipt_file,
        token=token,
        runner_pid=4242,
        status="claiming",
        names=names,
        run_indices=[1],
        claimed=[],
    )
    monkeypatch.setattr(runner, "DEFAULT_ABORT_TIMEOUT_SEC", 0.001)
    monkeypatch.setattr(runner, "kill_process", lambda *_args, **_kwargs: True)

    outcome = runner._abort_submission(
        _runner_process(),
        process_create_time=10.0,
        receipt_file=receipt_file,
        abort_file=abort_file,
        token=token,
        names=names,
        run_indices=[1],
        reason="test timeout",
    )

    assert outcome.result == SubmissionResult("unresolved", (), ("batch-a",))


def test_submission_receipt_requires_exact_token_pid_shape_and_task_partition(tmp_path):
    token = "2" * 32
    names = ["batch-a", "batch-b"]
    receipt, _ = submission_control_paths(str(tmp_path), token)
    valid = {
        "schema_version": SCHEMA_VERSION,
        "submission_token": token,
        "runner_pid": 4242,
        "status": "accepted",
        "run_indices": [1, 2],
        "claimed": names,
        "unclaimed": [],
        "detail": "",
    }
    invalid_payloads = [
        {**valid, "schema_version": 1.0},
        {**valid, "submission_token": "3" * 32},
        {**valid, "runner_pid": 4242.0},
        {**valid, "runner_pid": 0},
        {**valid, "run_indices": [1]},
        {**valid, "run_indices": [1, 2.0]},
        {**valid, "run_indices": [2, 1]},
        {**valid, "claimed": ["batch-a"], "unclaimed": []},
        {**valid, "claimed": ["batch-b", "batch-a"]},
        {**valid, "claimed": ["batch-a"], "unclaimed": ["batch-b"]},
    ]
    for payload in invalid_payloads:
        atomic_write_json(receipt, payload)
        assert read_submission_receipt(
            receipt,
            token=token,
            names=names,
            run_indices=[1, 2],
        ) is None

    atomic_write_json(receipt, valid)
    parsed = read_submission_receipt(
        receipt,
        token=token,
        names=names,
        run_indices=[1, 2],
    )
    assert parsed is not None
    assert parsed.runner_pid == 4242
    assert parsed.status == "accepted"
    assert parsed.claimed == ("batch-a", "batch-b")
    assert parsed.run_indices == (1, 2)


def test_submission_interrupt_persists_abort_and_ignores_repeated_sigint(tmp_path, monkeypatch):
    from pyruns.cli import runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("interrupt", {"value": 1})
    captured: dict[str, object] = {}
    sleep_calls = 0

    def fake_popen(command, _env):
        captured["command"] = command
        return _runner_process()

    def interrupt_then_acknowledge(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls <= 2:
            raise KeyboardInterrupt
        command = captured["command"]
        assert isinstance(command, list)
        token, names, _run_indices, _receipt, abort = _submission_from_command(command)
        payload = json.loads(abort.read_text(encoding="utf-8"))
        assert payload["submission_token"] == token
        assert payload["status"] == "requested"
        captured["abort_seen"] = True
        _write_test_receipt(command, "aborted", [])

    monkeypatch.setattr(runner, "_detached_popen", fake_popen)
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})
    monkeypatch.setattr(runner, "get_process_create_time", lambda _pid: 10.0)
    monkeypatch.setattr(runner.time, "sleep", interrupt_then_acknowledge)

    with pytest.raises(KeyboardInterrupt):
        runner.submit_cli_tasks(
            _submission_manager(tasks_dir, [task]),
            ["interrupt"],
            expected_runs={"interrupt": 1},
            startup_timeout=1.0,
        )

    assert captured["abort_seen"] is True
    command = captured["command"]
    assert isinstance(command, list)
    _, receipt, abort = _submission_control_from_command(command)
    assert not receipt.exists()
    assert not abort.exists()


def test_submission_interrupt_during_process_identity_capture_still_aborts(
    tmp_path,
    monkeypatch,
):
    from pyruns.cli import runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("identity-race", {"value": 1})
    captured: dict[str, object] = {}
    identity_calls = 0

    def fake_popen(command, _env):
        captured["command"] = command
        return _runner_process()

    def interrupted_identity(_pid):
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls == 1:
            raise KeyboardInterrupt
        return 10.0

    def acknowledge_abort(_seconds):
        command = captured["command"]
        assert isinstance(command, list)
        _, _, _, _receipt, abort = _submission_from_command(command)
        assert abort.is_file()
        captured["abort_seen"] = True
        _write_test_receipt(command, "aborted", [])

    monkeypatch.setattr(runner, "_detached_popen", fake_popen)
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})
    monkeypatch.setattr(runner, "get_process_create_time", interrupted_identity)
    monkeypatch.setattr(runner.time, "sleep", acknowledge_abort)

    with pytest.raises(runner.SubmissionInterrupted) as raised:
        runner.submit_cli_tasks(
            _submission_manager(tasks_dir, [task]),
            ["identity-race"],
            expected_runs={"identity-race": 1},
        )

    assert raised.value.result.status == "aborted"
    assert raised.value.runner_pid == 4242
    assert captured["abort_seen"] is True


def test_submission_cleanup_interrupt_preserves_the_first_terminal_result(
    tmp_path,
    monkeypatch,
):
    from pyruns.cli import runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("timeout", {"value": 1})
    abort_calls = 0

    monkeypatch.setattr(runner, "_detached_popen", lambda *_args: _runner_process())
    monkeypatch.setattr(runner, "get_follow_shell_runtime", lambda: {})
    monkeypatch.setattr(runner, "get_process_create_time", lambda _pid: 10.0)
    monkeypatch.setattr(runner, "_read_receipt", lambda *_args, **_kwargs: None)
    monotonic_values = iter([0.0, 1.0])
    monkeypatch.setattr(
        runner,
        "time",
        SimpleNamespace(
            monotonic=lambda: next(monotonic_values),
            sleep=runner.time.sleep,
        ),
    )

    def interrupted_abort(*_args, **_kwargs):
        nonlocal abort_calls
        abort_calls += 1
        status = "aborted" if abort_calls == 1 else "unresolved"
        result = SubmissionResult(status, (), ("timeout",))
        return runner._AbortOutcome(result, interrupted=True)

    monkeypatch.setattr(runner, "_abort_submission", interrupted_abort)

    with pytest.raises(runner.SubmissionInterrupted) as raised:
        runner.submit_cli_tasks(
            _submission_manager(tasks_dir, [task]),
            ["timeout"],
            expected_runs={"timeout": 1},
            startup_timeout=0,
        )

    assert abort_calls == 1
    assert raised.value.result.status == "aborted"


def test_unresolved_interrupt_cancels_only_exact_submission_owner(
    tmp_path,
    monkeypatch,
):
    from pyruns.cli import commands, runner

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    generator = TaskGenerator(root_dir=str(tasks_dir))
    owned = generator.create_task("owned", {"value": 1})
    foreign = generator.create_task("foreign", {"value": 2})
    token = "7" * 32
    runner_pid = 4242
    cancel_requests: list[tuple[str, str | None, int | None]] = []

    class FakeManager:
        def request_task_cancel(
            self,
            name,
            *,
            expected_runner_id=None,
            expected_run_index=None,
        ):
            cancel_requests.append((name, expected_runner_id, expected_run_index))
            return True

    def interrupted_submit(*_args, **_kwargs):
        raise runner.SubmissionInterrupted(
            SubmissionResult("unresolved", ("owned",), ("foreign",)),
            token,
            runner_pid,
        )

    def task_info(task_dir):
        name = Path(task_dir).name
        owner = runner_pid if name == "owned" else 9999
        return {
            "name": name,
            "status": "queued",
            "runner_id": f"host:{owner}:{token}",
        }

    def wait_for_cancelled(tasks, identities, **_kwargs):
        assert [task["name"] for task in tasks] == ["owned"]
        assert identities["owned"].run_index == 1
        assert identities["owned"].runner_id == f"host:{runner_pid}:{token}"
        return [{"name": "owned", "status": "cancelled"}]

    monkeypatch.setattr(commands, "submit_cli_tasks", interrupted_submit)
    monkeypatch.setattr(commands, "load_task_info", task_info)
    monkeypatch.setattr(commands, "_wait_for_task_records", wait_for_cancelled)

    with pytest.raises(runner.SubmissionInterrupted):
        commands._submit_and_wait(
            SimpleNamespace(json_output=False, program="pyr"),
            FakeManager(),
            [owned, foreign],
            workers=2,
            detach=True,
        )

    assert cancel_requests == [("owned", f"host:{runner_pid}:{token}", 1)]


def test_detached_runner_aborts_before_claiming_when_request_already_exists(tmp_path, monkeypatch):
    from pyruns.cli import detached_runner

    token = "4" * 32
    tasks_dir = tmp_path / TASKS_DIR
    receipt_file, abort_file = submission_control_paths(str(tasks_dir), token)
    write_abort_request(abort_file, token=token, reason="test timeout")
    payload_path = _write_test_submission(tmp_path, token, ["pending"], [1])

    class UnexpectedTaskManager:
        def __init__(self, **_kwargs):
            pytest.fail("runner must not discover or claim tasks after an early abort")

    monkeypatch.setattr(
        detached_runner,
        "_parse_args",
        lambda: SimpleNamespace(
            workspace=str(tmp_path),
            jobs=1,
            submission_token=token,
        ),
    )
    monkeypatch.setattr(detached_runner, "TaskManager", UnexpectedTaskManager)

    assert detached_runner.main() == 2
    assert not payload_path.exists()
    receipt = read_submission_receipt(
        receipt_file,
        token=token,
        names=["pending"],
        run_indices=[1],
    )
    assert receipt is not None
    assert receipt.status == "aborted"
    assert receipt.claimed == ()


def test_detached_runner_cancels_claimed_tasks_before_reporting_partial(tmp_path, monkeypatch):
    from pyruns.cli import detached_runner

    token = "5" * 32
    task_a = {"name": "batch-a", "dir": str(tmp_path / TASKS_DIR / "batch-a"), "status": "pending"}
    task_b = {"name": "batch-b", "dir": str(tmp_path / TASKS_DIR / "batch-b"), "status": "pending"}
    tasks = {"batch-a": task_a, "batch-b": task_b}
    states = {
        task_a["dir"]: {"status": "pending"},
        task_b["dir"]: {"status": "pending"},
    }
    events: list[str] = []
    payload_path = _write_test_submission(
        tmp_path,
        token,
        ["batch-a", "batch-b"],
        [1, 1],
    )

    class FakeTaskManager:
        def __init__(self, **_kwargs):
            self.runner_id = "local:4242:token"

        def get_task(self, name):
            return tasks.get(name)

        def start_batch_tasks(
            self,
            names,
            max_workers=None,
            expected_run_indices=None,
        ):
            name = names[0]
            events.append(f"claim:{name}")
            assert max_workers == 1
            assert expected_run_indices == {name: 1}
            if name == "batch-a":
                states[task_a["dir"]] = {
                    "status": "queued",
                    "runner_id": self.runner_id,
                }
                return [name]
            return []

        def request_task_cancel(
            self,
            name,
            *,
            expected_runner_id=None,
            expected_run_index=None,
        ):
            assert expected_runner_id == self.runner_id
            assert expected_run_index == 1
            events.append(f"cancel:{name}")
            states[tasks[name]["dir"]] = {"status": "cancelled"}
            return True

        def shutdown(self):
            events.append("shutdown")

    real_write_receipt = detached_runner.write_submission_receipt

    def recording_write_receipt(*args, **kwargs):
        events.append(f"receipt:{kwargs['status']}")
        return real_write_receipt(*args, **kwargs)

    monkeypatch.setattr(
        detached_runner,
        "_parse_args",
        lambda: SimpleNamespace(
            workspace=str(tmp_path),
            jobs=1,
            submission_token=token,
        ),
    )
    monkeypatch.setattr(detached_runner, "TaskManager", FakeTaskManager)
    monkeypatch.setattr(detached_runner, "load_task_info", lambda task_dir: states.get(task_dir))
    monkeypatch.setattr(detached_runner, "write_submission_receipt", recording_write_receipt)

    assert detached_runner.main() == 2
    assert not payload_path.exists()
    assert states[task_a["dir"]]["status"] == "cancelled"
    assert events.index("cancel:batch-a") < events.index("receipt:partial")
    assert events[-1] == "shutdown"

    receipt_file, _ = submission_control_paths(str(tmp_path / TASKS_DIR), token)
    receipt = read_submission_receipt(
        receipt_file,
        token=token,
        names=["batch-a", "batch-b"],
        run_indices=[1, 1],
    )
    assert receipt is not None
    assert receipt.status == "partial"
    assert receipt.claimed == ("batch-a",)
    assert receipt.unclaimed == ("batch-b",)


def test_detached_runner_reports_unresolved_when_partial_cleanup_is_not_confirmed(
    tmp_path,
    monkeypatch,
):
    from pyruns.cli import detached_runner

    token = "6" * 32
    names = ["batch-a", "batch-b"]
    task_dir = str(tmp_path / TASKS_DIR / "batch-a")
    receipt_file, _ = submission_control_paths(str(tmp_path / TASKS_DIR), token)

    class FakeTaskManager:
        runner_id = "local:4242:token"

        def request_task_cancel(self, _name, **_kwargs):
            pytest.fail("an ownerless active task must not receive a cancel request")

    monkeypatch.setattr(
        detached_runner,
        "load_task_info",
        lambda _task_dir: {"status": "queued"},
    )
    monkeypatch.setattr(detached_runner, "_CLEANUP_TIMEOUT_SEC", 0.0)

    assert detached_runner._stop_and_report(
        FakeTaskManager(),
        selected={"batch-a": {"name": "batch-a", "dir": task_dir}},
        receipt_file=receipt_file,
        token=token,
        names=names,
        run_indices=[1, 1],
        claimed=["batch-a"],
        stopped_status="partial",
        detail="test claim conflict",
    ) == 2

    receipt = read_submission_receipt(
        receipt_file,
        token=token,
        names=names,
        run_indices=[1, 1],
    )
    assert receipt is not None
    assert receipt.status == "unresolved"


def test_detached_runner_does_not_cancel_foreign_rerun_of_claimed_task(
    tmp_path,
    monkeypatch,
):
    from pyruns.cli import detached_runner

    token = "c" * 32
    task_dir = str(tmp_path / TASKS_DIR / "claimed")
    receipt_file, _ = submission_control_paths(str(tmp_path / TASKS_DIR), token)
    cancel_requests: list[str] = []

    class FakeTaskManager:
        runner_id = "local:4242:token"

        def request_task_cancel(self, name, **_kwargs):
            cancel_requests.append(name)
            return True

    monkeypatch.setattr(
        detached_runner,
        "load_task_info",
        lambda _task_dir: {
            "status": "running",
            "runner_id": "foreign:9999:other",
        },
    )

    assert detached_runner._stop_and_report(
        FakeTaskManager(),
        selected={"claimed": {"name": "claimed", "dir": task_dir}},
        receipt_file=receipt_file,
        token=token,
        names=["claimed"],
        run_indices=[1],
        claimed=["claimed"],
        stopped_status="aborted",
        detail="test abort",
    ) == 2

    receipt = read_submission_receipt(
        receipt_file,
        token=token,
        names=["claimed"],
        run_indices=[1],
    )
    assert receipt is not None
    assert receipt.status == "unresolved"
    assert cancel_requests == []


def test_owned_cancel_guard_rechecks_runner_and_run_inside_task_lock(tmp_path):
    from unittest.mock import patch

    from pyruns.core.task_manager import TaskManager

    tasks_dir = tmp_path / TASKS_DIR
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("claimed", {"value": 1})
    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    update_task_info(
        task["dir"],
        lambda info: info.update(
            {
                "status": "running",
                "runner_id": "foreign:9999:other",
            }
        ),
    )

    assert manager.request_task_cancel(
        "claimed",
        expected_runner_id=manager.runner_id,
    ) is False
    info = load_task_info(task["dir"])
    assert info.get("cancel_requested_at") is None

    update_task_info(
        task["dir"],
        lambda current: current.update(
            {
                "status": "running",
                "run_index": 2,
                "runner_id": manager.runner_id,
                "run_statuses": ["completed", "running"],
            }
        ),
    )

    assert manager.request_task_cancel(
        "claimed",
        expected_runner_id=manager.runner_id,
        expected_run_index=1,
    ) is False
    info = load_task_info(task["dir"])
    assert info["status"] == "running"
    assert info["run_index"] == 2
    assert info.get("cancel_requested_at") is None


def test_cancel_side_effect_does_not_touch_a_newer_run(tmp_path, monkeypatch):
    from unittest.mock import patch

    from pyruns.core.task_manager import TaskManager

    tasks_dir = tmp_path / TASKS_DIR
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("race", {"value": 1})
    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(
            tasks_dir=str(tasks_dir),
            lazy_scan=False,
            owns_task_lifecycle=False,
        )

    update_task_info(
        task["dir"],
        lambda info: info.update(
            {
                "status": "running",
                "run_index": 1,
                "runner_id": manager.runner_id,
                "run_statuses": ["running"],
            }
        ),
    )
    real_cancel = manager.cancel_task

    def advance_run_before_side_effect(name, **kwargs):
        def _advance(info):
            info.update(
                {
                    "status": "running",
                    "run_index": 2,
                    "runner_id": manager.runner_id,
                    "run_statuses": ["completed", "running"],
                }
            )
            info.pop("cancel_requested_at", None)
            info.pop("_pending_stop_summary", None)

        update_task_info(task["dir"], _advance)
        return real_cancel(name, **kwargs)

    monkeypatch.setattr(manager, "cancel_task", advance_run_before_side_effect)
    try:
        assert manager.request_task_cancel(
            "race",
            expected_runner_id=manager.runner_id,
            expected_run_index=1,
        ) is False
    finally:
        manager.shutdown()

    info = load_task_info(task["dir"])
    assert info["status"] == "running"
    assert info["run_index"] == 2
    assert info["run_statuses"] == ["completed", "running"]
    assert info.get("cancel_requested_at") is None
    assert info.get("_pending_stop_summary") is None


def test_ownerless_active_task_keeps_an_exact_empty_runner_identity(tmp_path):
    from pyruns.cli import commands

    tasks_dir = tmp_path / TASKS_DIR
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("orphan", {"value": 1})
    update_task_info(
        task["dir"],
        lambda info: info.update({"status": "queued"}),
    )

    identity = commands._capture_task_run_identity(task)

    assert identity.run_index == 1
    assert identity.runner_id == ""
    assert identity.started_queued is True


def test_cancel_reconciliation_uses_the_locked_run_index(tmp_path):
    from unittest.mock import patch

    from pyruns.core.task_manager import TaskManager

    tasks_dir = tmp_path / TASKS_DIR
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("stale-cache", {"value": 1})
    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    dead_runner = "missing-host:9999:dead-token"
    update_task_info(
        task["dir"],
        lambda info: info.update(
            {
                "status": "running",
                "run_index": 2,
                "runner_id": dead_runner,
                "run_statuses": ["completed", "running"],
            }
        ),
    )
    with manager._lock:
        cached = manager._resolve_identifier_locked("stale-cache")
        assert cached is not None
        cached["status"] = "running"
        cached["run_index"] = 3

    try:
        assert manager.request_task_cancel(
            "stale-cache",
            expected_runner_id=dead_runner,
            expected_run_index=2,
        ) is True
    finally:
        manager.shutdown()

    info = load_task_info(task["dir"])
    assert info["status"] == "failed"
    assert info["run_index"] == 2
    assert info["run_statuses"] == ["completed", "failed"]


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
        workers=999999,
        detach=True,
    )

    assert result == 1
    assert captured["max_workers"] == 2
    error = capsys.readouterr().err
    assert "submission partial" in error
    assert "Claimed: batch-a" in error
    assert "Unclaimed: batch-b" in error


def test_unresolved_submission_reports_quoted_context_aware_recovery_commands(
    tmp_path,
    monkeypatch,
    capsys,
):
    from pyruns.cli import commands

    tasks_dir = tmp_path / "workspace" / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    task_name = "uncertain; echo injected"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task(task_name, {"value": 1})
    context = SimpleNamespace(
        json_output=False,
        program="pyr",
        directory=str(tmp_path / "project with spaces"),
    )
    monkeypatch.setattr(
        commands,
        "get_shell_runtime_for_workspace",
        lambda _workspace: {"terminal_kind": "bash"},
    )
    monkeypatch.setattr(
        commands,
        "submit_cli_tasks",
        lambda *_args, **_kwargs: SubmissionResult(
            "unresolved",
            (),
            (task_name,),
        ),
    )

    result = commands._submit_and_wait(
        context,
        _submission_manager(tasks_dir, [task]),
        [task],
        workers=1,
        detach=True,
    )

    assert result == 1
    error = capsys.readouterr().err
    assert "cleanup could not be verified" in error
    lines = error.splitlines()
    status_command = next(
        line.removeprefix("Check task state: ")
        for line in lines
        if line.startswith("Check task state: ")
    )
    stop_command = next(
        line.removeprefix("Stop active tasks: ")
        for line in lines
        if line.startswith("Stop active tasks: ")
    )
    expected_context = [
        "pyr",
        "-C",
        context.directory,
        "-w",
        str(tasks_dir.parent).replace("\\", "/"),
    ]
    assert shlex.split(status_command) == [*expected_context, "status"]
    assert shlex.split(stop_command) == [*expected_context, "stop", task_name]


def test_log_writer_stops_after_stdout_pipe_closes(tmp_path, monkeypatch):
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

    with pytest.raises(BrokenPipeError):
        commands._write_available_log(str(log_path), 0)


def test_test_tmp_root_moves_outside_ancestor_pyruns_project(
    tmp_path,
    isolated_tmp_root_resolver,
):
    project = tmp_path / "polluted"
    (project / "_pyruns_").mkdir(parents=True)
    candidate = project / "pyruns-tests"

    assert isolated_tmp_root_resolver(candidate) == tmp_path / "pyruns-tests"


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
