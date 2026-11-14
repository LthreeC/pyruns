"""Shared-workspace readers must converge after a worker exits."""

import os
import time

import pytest

from pyruns.core import task_manager as manager_module
from pyruns.core.task_generator import TaskGenerator
from pyruns.core.task_manager import TaskManager
from pyruns.utils.info_io import load_task_metadata, update_task_metadata
from pyruns.utils import run_result


@pytest.fixture
def shared_run(tmp_path, monkeypatch):
    monkeypatch.setattr(TaskManager, "_scheduler_loop", lambda self: None)
    tasks = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks)).create_task("shared", {"value": 1})
    update_task_metadata(task["dir"], lambda info: info.update(
        status="running", run_index=1, run_statuses=["running"],
        start_times=["2026-11-14_00-00-00"], finish_times=[""],
        pids=[987654321], pid_create_times=[1000.0],
        runner_host="worker-host", runner_id="worker-host:123:token",
        lease_until=time.time() - 60, _attempt_id="attempt-one",
        run_attempt_ids=["attempt-one"],
    ))
    readers = []

    def reader(host="observer-host", reconcile=True):
        manager = TaskManager(tasks_dir=str(tasks), lazy_scan=True,
                              owns_task_lifecycle=False, reconcile_unowned_running=reconcile)
        manager.runner_host = host
        manager.scan_disk()
        readers.append(manager)
        return manager

    yield task["dir"], reader
    for manager in readers:
        manager.shutdown()


@pytest.mark.parametrize("initial_status", ["running", "queued"])
@pytest.mark.parametrize("final_status", ["completed", "cancelled", "failed"])
def test_active_reader_observes_completion_with_cached_attributes(
    shared_run, monkeypatch, initial_status, final_status,
):
    task_dir, reader = shared_run
    update_task_metadata(task_dir, lambda info: info.update(status=initial_status))
    manager = reader()
    old_stat = os.stat(os.path.join(task_dir, "task_info.json"))
    old_signature = manager._tasks_by_name["shared"]["_info_signature"]
    update_task_metadata(task_dir, lambda info: info.update(
        status=final_status, run_statuses=[final_status], finish_times=["2026-11-14_00-00-01"],
    ))
    monkeypatch.setattr(manager, "_probe_refresh_task", lambda *_args, **_kwargs: (
        old_stat, old_signature, False, None,
    ))
    monkeypatch.setattr(manager, "_stat_signature", lambda _stat: old_signature)

    assert manager.refresh_from_disk() is True
    assert manager.get_task("shared")["status"] == final_status
    assert reader("another-observer").get_task("shared")["status"] == final_status
    assert manager.active_task_count() == 0


def test_expired_local_cancellation_recovers_to_cancelled(shared_run, monkeypatch):
    task_dir, reader = shared_run
    update_task_metadata(task_dir, lambda info: info.update(cancel_requested_at="2026-11-14_00-00-01"))
    monkeypatch.setattr(manager_module, "process_identity_matches", lambda *_: False)

    manager = reader("worker-host")

    assert manager.get_task("shared")["status"] == "cancelled"
    assert load_task_metadata(task_dir)["run_statuses"] == ["cancelled"]
    assert reader().get_task("shared")["status"] == "cancelled"


def _finished_result(info, status="completed"):
    return {
        "run_index": info["run_index"], "runner_id": info["runner_id"],
        "attempt_id": info.get("_attempt_id", ""), "status": status,
        "start_time": "2026-11-14_00-00-00", "finish_time": "2026-11-14_00-00-01",
        "pid": 987654321, "pid_create_time": 1000.0,
        "duration_seconds": 1.0, "exit_code": 0 if status == "completed" else -15,
    }


@pytest.mark.parametrize("status", ["completed", "cancelled", "failed"])
def test_remote_reader_replays_result_without_local_pid_checks(shared_run, monkeypatch, status):
    task_dir, reader = shared_run
    update_task_metadata(task_dir, lambda info: info.update(
        lease_until=time.time() + 60, _gpu_assignment={"gpu_ids": [0]}, _scheduled_env={"CUDA_VISIBLE_DEVICES": "0"},
    ))
    run_result.write_run_result(task_dir, _finished_result(load_task_metadata(task_dir), status))
    monkeypatch.setattr(manager_module, "process_identity_matches", lambda *_: pytest.fail("remote PID probe"))
    monkeypatch.setattr(manager_module, "is_pid_running", lambda *_: pytest.fail("remote PID probe"))

    manager = reader(reconcile=False)

    assert manager.get_task("shared")["status"] == status
    current = load_task_metadata(task_dir)
    assert current["run_statuses"] == [status]
    assert current["exit_codes"] == [0 if status == "completed" else -15]
    assert current["durations"] == [1.0]
    assert "runner_id" not in current
    assert "_gpu_assignment" not in current
    assert "_scheduled_env" not in current
    assert reader("second-host").get_task("shared")["status"] == status


def test_result_replay_obeys_durable_cancel_request(shared_run):
    task_dir, reader = shared_run
    run_result.write_run_result(task_dir, _finished_result(load_task_metadata(task_dir)))
    update_task_metadata(task_dir, lambda info: info.update(cancel_requested_at="2026-11-14_00-00-01"))
    assert reader().get_task("shared")["status"] == "cancelled"


@pytest.mark.parametrize("change", ["attempt", "runner", "run_index"])
def test_stale_result_cannot_finalize_a_new_execution(shared_run, change):
    task_dir, reader = shared_run
    run_result.write_run_result(task_dir, _finished_result(load_task_metadata(task_dir)))
    updates = {
        "attempt": {"_attempt_id": "attempt-two", "run_attempt_ids": ["attempt-two"]},
        "runner": {"runner_id": "new-runner"},
        "run_index": {"run_index": 2, "run_statuses": ["completed", "running"]},
    }
    update_task_metadata(task_dir, lambda info: info.update(updates[change]))
    assert reader().get_task("shared")["status"] == "running"


def test_replay_rechecks_attempt_under_lock(shared_run, monkeypatch):
    task_dir, _reader = shared_run
    before = load_task_metadata(task_dir)
    run_result.write_run_result(task_dir, _finished_result(before))
    original_update = run_result.update_task_metadata

    def replace_before_lock(task_dir, updater, **kwargs):
        original_update(task_dir, lambda info: info.update(_attempt_id="replacement", notes="new notes"))
        return original_update(task_dir, updater, **kwargs)

    monkeypatch.setattr(run_result, "update_task_metadata", replace_before_lock)
    current, recovered = run_result.recover_run_result(task_dir, before)
    assert recovered is False
    assert current["status"] == "running"
    assert current["_attempt_id"] == "replacement"
    assert current["notes"] == "new notes"


def test_failed_result_replay_keeps_shared_status_then_retries(shared_run, monkeypatch):
    task_dir, reader = shared_run
    run_result.write_run_result(task_dir, _finished_result(load_task_metadata(task_dir)))
    original_update = run_result.update_task_metadata

    def busy_metadata(*_args, **_kwargs):
        raise TimeoutError("metadata busy")

    monkeypatch.setattr(run_result, "update_task_metadata", busy_metadata)
    manager = reader()
    assert manager.get_task("shared")["status"] == load_task_metadata(task_dir)["status"] == "running"
    assert "metadata busy" in manager.get_task("shared")["_load_error"]

    monkeypatch.setattr(run_result, "update_task_metadata", original_update)
    assert manager.refresh_from_disk() is True
    assert manager.get_task("shared")["status"] == "completed"
    assert manager.get_task("shared")["_load_error"] == ""


@pytest.mark.skipif(os.name == "nt", reason="POSIX shared filesystem locking")
def test_posix_reader_recovers_abandoned_peer_metadata_lock(shared_run):
    import hashlib
    import sys
    from pathlib import Path

    task_dir, reader = shared_run
    peer_domain = hashlib.blake2b(
        "\0".join((os.name, sys.platform, "worker-host", "")).encode(), digest_size=16,
    ).hexdigest()
    run_result.write_run_result(task_dir, _finished_result(load_task_metadata(task_dir)))
    Path(task_dir, ".task_info.json.lock").write_text(f"123 1 worker-host 1 guard-v2:{peer_domain}")
    assert reader().get_task("shared")["status"] == "completed"
