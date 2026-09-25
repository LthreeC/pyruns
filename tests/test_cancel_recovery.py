"""Cancellation must not publish a finished run while its process survives."""

import json
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from pyruns.core import task_manager as task_manager_module
from pyruns.core.task_generator import TaskGenerator
from pyruns.core.task_manager import TaskManager
from pyruns.utils.info_io import load_task_metadata, update_task_metadata
from pyruns.utils.process_utils import is_pid_running, kill_process, process_identity_matches


@pytest.fixture
def expired_run(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("orphan", {"value": 1})
    monkeypatch.setattr(TaskManager, "_scheduler_loop", lambda self: None)
    manager = TaskManager(
        tasks_dir=str(tasks_dir), lazy_scan=False, owns_task_lifecycle=False,
    )
    update_task_metadata(
        task["dir"],
        lambda info: info.update(
            status="running",
            run_index=1,
            run_statuses=["running"],
            start_times=["2026-09-25_00-00-00"],
            finish_times=[""],
            pids=[987654321],
            pid_create_times=[1000.0],
            runner_id="expired-runner",
            runner_host=manager.runner_host,
            lease_until=time.time() - 60,
        ),
    )
    try:
        yield manager, Path(task["dir"])
    finally:
        manager.shutdown()


@pytest.mark.parametrize("termination_succeeds", [False, True])
def test_expired_local_run_stays_active_until_process_tree_stops(
    expired_run, monkeypatch, termination_succeeds,
):
    manager, task_dir = expired_run
    alive = True
    kills = []
    monkeypatch.setattr(task_manager_module, "process_identity_matches", lambda *_: alive)

    def kill(pid, expected_create_time=None):
        nonlocal alive
        kills.append((pid, expected_create_time))
        during_stop = load_task_metadata(str(task_dir))
        assert during_stop["status"] == "running"
        assert during_stop["run_statuses"] == ["running"]
        assert during_stop["cancel_requested_at"]
        alive = not termination_succeeds
        return termination_succeeds

    monkeypatch.setattr(task_manager_module, "kill_process", kill)

    assert manager.request_task_cancel("orphan") is termination_succeeds

    info = load_task_metadata(str(task_dir))
    assert kills == [(987654321, 1000.0)]
    assert info["status"] == ("cancelled" if termination_succeeds else "running")
    assert info["run_statuses"] == [info["status"]]
    assert bool(info["finish_times"][0]) is termination_succeeds
    assert info["cancel_requested_at"]
    if termination_succeeds:
        assert "runner_id" not in info
    else:
        assert info["runner_id"] == "expired-runner"


@pytest.mark.parametrize("runner_host", ["other-host", ""])
def test_expired_remote_or_unknown_host_keeps_cancellation_pending(
    expired_run, monkeypatch, runner_host,
):
    manager, task_dir = expired_run
    update_task_metadata(str(task_dir), lambda info: info.update(runner_host=runner_host))
    monkeypatch.setattr(
        task_manager_module, "kill_process",
        lambda *_args, **_kwargs: pytest.fail("must not kill a process on an unverified host"),
    )

    assert manager.request_task_cancel("orphan") is True

    info = load_task_metadata(str(task_dir))
    assert info["status"] == "running"
    assert info["run_statuses"] == ["running"]
    assert not info["finish_times"][0]
    assert info["cancel_requested_at"]
    assert info["runner_id"] == "expired-runner"


def test_expired_local_run_without_pid_creation_time_remains_active(expired_run, monkeypatch):
    manager, task_dir = expired_run
    update_task_metadata(str(task_dir), lambda info: info.update(pid_create_times=[]))
    monkeypatch.setattr(task_manager_module, "is_pid_running", lambda _pid: True)
    monkeypatch.setattr(
        task_manager_module, "kill_process",
        lambda *_args, **_kwargs: pytest.fail("a PID alone is not a process identity"),
    )

    assert manager.request_task_cancel("orphan") is True

    info = load_task_metadata(str(task_dir))
    assert info["status"] == "running"
    assert info["cancel_requested_at"]


def test_expired_local_run_does_not_signal_a_reused_pid(expired_run, monkeypatch):
    manager, task_dir = expired_run
    monkeypatch.setattr(task_manager_module, "is_pid_running", lambda _pid: True)
    monkeypatch.setattr(task_manager_module, "process_identity_matches", lambda *_: False)
    monkeypatch.setattr(
        task_manager_module, "kill_process",
        lambda *_args, **_kwargs: pytest.fail("the original process has already exited"),
    )

    assert manager.request_task_cancel("orphan") is True

    info = load_task_metadata(str(task_dir))
    assert info["status"] == "failed"
    assert info["run_statuses"] == ["failed"]


def test_expired_run_stop_write_failure_keeps_the_request_retryable(expired_run, monkeypatch):
    manager, task_dir = expired_run
    monkeypatch.setattr(task_manager_module, "process_identity_matches", lambda *_: True)
    monkeypatch.setattr(task_manager_module, "kill_process", lambda *_args, **_kwargs: True)

    def fail_terminal_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_mark_failed_on_disk", fail_terminal_write)

    assert manager.request_task_cancel("orphan") is False

    info = load_task_metadata(str(task_dir))
    assert info["status"] == "running"
    assert info["cancel_requested_at"]
    assert info["runner_id"] == "expired-runner"
    assert not info["finish_times"][0]


def test_expired_run_stop_does_not_finalize_a_concurrent_rerun(expired_run, monkeypatch):
    manager, task_dir = expired_run
    alive = True
    kills = []
    monkeypatch.setattr(task_manager_module, "process_identity_matches", lambda *_: alive)

    def kill(pid, expected_create_time=None):
        nonlocal alive
        kills.append((pid, expected_create_time))
        alive = False
        update_task_metadata(
            str(task_dir),
            lambda info: info.update(
                status="running", run_index=2, runner_id="new-runner",
                run_statuses=["cancelled", "running"], lease_until=time.time() + 60,
            ),
        )
        return True

    monkeypatch.setattr(task_manager_module, "kill_process", kill)

    assert manager.request_task_cancel("orphan") is False

    info = load_task_metadata(str(task_dir))
    assert kills == [(987654321, 1000.0)]
    assert info["status"] == "running"
    assert info["run_index"] == 2
    assert info["run_statuses"] == ["cancelled", "running"]
    assert info["runner_id"] == "new-runner"


@pytest.mark.parametrize("change", ["rerun", "renew_lease"])
def test_expired_run_stop_rechecks_state_before_signalling(expired_run, monkeypatch, change):
    manager, task_dir = expired_run
    monkeypatch.setattr(task_manager_module, "process_identity_matches", lambda *_: True)
    monkeypatch.setattr(
        task_manager_module, "kill_process",
        lambda *_args, **_kwargs: pytest.fail("ownership was refreshed before signalling"),
    )

    def change_after_request():
        def update(info):
            info["lease_until"] = time.time() + 60
            if change == "rerun":
                info.update(
                    runner_id="new-runner", run_index=2,
                    run_statuses=["completed", "running"],
                )
        update_task_metadata(str(task_dir), update)

    monkeypatch.setattr(manager, "trigger_update", change_after_request)

    assert manager.request_task_cancel("orphan") is (change == "renew_lease")
    info = load_task_metadata(str(task_dir))
    assert info["status"] == "running"
    assert info["run_index"] == (2 if change == "rerun" else 1)


@pytest.mark.parametrize("launch", ["immediate", "batch", "queued"])
def test_rerun_clears_previous_run_cancellation_before_dispatch(expired_run, monkeypatch, launch):
    manager, task_dir = expired_run
    update_task_metadata(
        str(task_dir),
        lambda info: info.update(
            status="cancelled", run_statuses=["cancelled"],
            finish_times=["2026-09-25_00-00-01"], cancel_requested_at="old-request",
            _pending_stop_summary={"run_index": 1, "reason": "cancelled_by_user"},
        ),
    )
    manager.refresh_from_disk(task_ids=["orphan"], force_all=True)
    submitted = []

    def observe_dispatch(*_args, **_kwargs):
        submitted.append(load_task_metadata(str(task_dir)))

    monkeypatch.setattr(manager, "_submit_task", observe_dispatch)
    if launch == "immediate":
        assert manager.start_task_now("orphan") is True
    elif launch == "batch":
        assert manager.start_batch_tasks(["orphan"]) == ["orphan"]
    else:
        assert manager.rerun_task("orphan") is True
    info = load_task_metadata(str(task_dir))
    assert "cancel_requested_at" not in info
    assert "_pending_stop_summary" not in info
    assert info["run_statuses"][0] == "cancelled"
    if launch != "queued":
        assert submitted and submitted[0]["run_index"] == 2
        assert "cancel_requested_at" not in submitted[0]
        assert "_pending_stop_summary" not in submitted[0]


def test_queued_to_running_keeps_the_current_run_cancellation(expired_run):
    manager, task_dir = expired_run
    update_task_metadata(
        str(task_dir),
        lambda info: info.update(
            status="queued", runner_id=manager.runner_id,
            run_statuses=["completed"], finish_times=["2026-09-25_00-00-01"],
            cancel_requested_at="current-request",
        ),
    )
    manager.refresh_from_disk(task_ids=["orphan"], force_all=True)

    assert manager._sync_status_to_disk(
        "orphan", "running", run_index=2, expected_statuses={"queued"},
    ) is True

    info = load_task_metadata(str(task_dir))
    assert info["cancel_requested_at"] == "current-request"
    assert info["run_index"] == 2


def test_expired_local_run_cancellation_stops_real_process_tree(expired_run):
    manager, task_dir = expired_run
    ready = task_dir.parent.parent / "process-ready.json"
    code = """
import json, os, subprocess, sys, time
from pathlib import Path
import psutil
from pyruns.utils.process_utils import hidden_subprocess_kwargs
child = subprocess.Popen(
    [sys.executable, '-c', 'import time; time.sleep(30)'],
    **hidden_subprocess_kwargs(),
)
ready = Path(sys.argv[1])
temporary = ready.with_suffix('.tmp')
temporary.write_text(json.dumps({
    'root': [os.getpid(), psutil.Process().create_time()],
    'child': [child.pid, psutil.Process(child.pid).create_time()],
}))
temporary.replace(ready)
time.sleep(30)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(ready)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    launcher_birth = psutil.Process(process.pid).create_time()
    identities = {}
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert time.monotonic() < deadline, "workload did not start"
            time.sleep(0.02)
        identities = json.loads(ready.read_text())
        pid, created_at = identities["root"]
        update_task_metadata(
            str(task_dir),
            lambda info: info.update(pids=[pid], pid_create_times=[created_at]),
        )

        assert manager.request_task_cancel("orphan") is True

        info = load_task_metadata(str(task_dir))
        assert info["status"] == "cancelled"
        assert info["run_statuses"] == ["cancelled"]
        assert all(
            not (is_pid_running(identity[0]) and process_identity_matches(*identity))
            for identity in identities.values()
        )
        assert manager.rerun_task("orphan") is True
        rerun = load_task_metadata(str(task_dir))
        assert rerun["status"] == "queued"
        assert rerun["run_statuses"] == ["cancelled"]
    finally:
        for identity in [*identities.values(), (process.pid, launcher_birth)]:
            if process_identity_matches(*identity):
                kill_process(identity[0], expected_create_time=identity[1])
        process.wait(timeout=10)
