"""An unstarted cancelled queue must not become a later run with the same index."""

from concurrent.futures import Future

import pytest

from pyruns.cli import commands, detached_runner
from pyruns._config import RUN_LOGS_DIR
from pyruns.core import executor, task_manager as manager_module
from pyruns.core.task_manager import TaskManager, TaskStateConflict
from pyruns.utils.info_io import load_task_metadata, save_task_info, update_task_metadata


@pytest.mark.parametrize("later_run", [1, 2])
def test_detached_observer_rejects_completed_replacement_attempt(later_run):
    info = {
        "status": "completed", "run_index": later_run,
        "run_statuses": ["completed"] * later_run,
        "_attempt_id": "replacement", "run_attempt_ids": ["replacement"] * later_run,
    }
    assert detached_runner._submitted_run_status(
        info, 1, expected_runner_id="same-manager", expected_attempt_id="cancelled-queue",
    ) is None


def test_wait_rejects_completed_replacement_before_first_poll(monkeypatch):
    info = {
        "status": "completed", "run_index": 1, "run_statuses": ["completed"],
        "_attempt_id": "replacement", "run_attempt_ids": ["replacement"],
    }
    monkeypatch.setattr(commands, "load_task_metadata", lambda _: info)
    identity = commands._TaskRunIdentity(1, "same-manager", True, attempt_id="cancelled-queue")
    with pytest.raises(commands.CliError, match="identity changed"):
        commands._bound_task_record({"name": "task", "dir": "unused"}, identity)


def _manager(tmp_path, monkeypatch):
    task_dir = tmp_path / "tasks" / "task"
    task_dir.mkdir(parents=True)
    (task_dir / "config.yaml").write_text("value: 1\n", encoding="utf-8")
    save_task_info(str(task_dir), {"name": "task", "status": "pending", "created_at": "2026-01-01"})
    manager = TaskManager(str(task_dir.parent), lazy_scan=False, owns_task_lifecycle=False)
    monkeypatch.setattr(manager, "trigger_update", lambda: None)
    return manager, task_dir


def test_same_manager_cancelled_queue_gets_new_attempt_without_consuming_run(tmp_path, monkeypatch):
    manager, task_dir = _manager(tmp_path, monkeypatch)
    manager.max_workers = 1
    manager._batch_running_ids.add("blocker")
    assert manager.start_batch_tasks(["task"]) == ["task"]
    first = load_task_metadata(str(task_dir))
    assert first.get("_attempt_id")
    assert not first.get("run_attempt_ids")
    manager._mark_failed_on_disk(
        {"name": "task", "dir": str(task_dir), "run_index": 0},
        expected_statuses={"queued"}, expected_attempt_id=first["_attempt_id"],
        final_status="cancelled",
    )
    manager.scan_disk()
    assert manager.start_batch_tasks(["task"]) == ["task"]
    second = load_task_metadata(str(task_dir))
    assert second["runner_id"] == first["runner_id"] == manager.runner_id
    assert second["_attempt_id"] != first["_attempt_id"]
    assert second["run_index"] == 0
    assert not second.get("run_attempt_ids")
    assert manager._claim_task_for_run(manager.get_task("task"), 1, counts_for_batch=True)
    manager._mark_failed_on_disk(manager.get_task("task"), final_status="completed")
    completed = load_task_metadata(str(task_dir))
    assert completed["run_attempt_ids"] == [second["_attempt_id"]]
    assert detached_runner._submitted_run_status(
        completed, 1, expected_runner_id=manager.runner_id, expected_attempt_id=first["_attempt_id"],
    ) is None
    original = commands._TaskRunIdentity(1, manager.runner_id, True, first["_attempt_id"])
    with pytest.raises(commands.CliError, match="identity changed"):
        commands._bound_task_record({"name": "task", "dir": str(task_dir)}, original)


def test_stale_same_owner_worker_completion_cannot_fail_replacement(tmp_path, monkeypatch):
    manager, task_dir = _manager(tmp_path, monkeypatch)
    assert manager._sync_status_to_disk("task", "running", expected_statuses={"pending"})
    current = load_task_metadata(str(task_dir))
    replacement_id = current.get("_attempt_id")
    future = Future()
    future.set_exception(RuntimeError("old queued worker failed"))
    manager._on_task_done(
        future, "task", expected_runner_id=manager.runner_id,
        expected_run_index=1, expected_attempt_id="old-attempt",
    )
    result = load_task_metadata(str(task_dir))
    assert result["status"] == "running"
    assert result["_attempt_id"] == replacement_id
    assert "task" in manager._running_ids


def test_stop_compare_and_swap_rejects_replacement_attempt(tmp_path, monkeypatch):
    manager, task_dir = _manager(tmp_path, monkeypatch)
    assert manager._sync_status_to_disk("task", "running", expected_statuses={"pending"})
    with pytest.raises(TaskStateConflict, match="attempt changed"):
        manager._mark_failed_on_disk(
            {"name": "task", "dir": str(task_dir), "run_index": 1},
            expected_runner_id=manager.runner_id, expected_run_index=1,
            expected_attempt_id="old-attempt", final_status="cancelled",
        )
    assert load_task_metadata(str(task_dir))["status"] == "running"


def test_wait_keeps_completed_original_run_when_next_attempt_starts(monkeypatch):
    info = {
        "status": "running", "run_index": 2, "runner_id": "same-manager",
        "_attempt_id": "new", "run_attempt_ids": ["original", "new"],
        "run_statuses": ["completed", "running"],
    }
    monkeypatch.setattr(commands, "load_task_metadata", lambda _: info)
    monkeypatch.setattr(commands, "_selected_run_record", lambda *_a, **_k: {"status": "completed"})
    monkeypatch.setattr(commands, "_task_record", lambda *_a, **_k: {})
    identity = commands._TaskRunIdentity(1, "same-manager", attempt_id="original")
    assert commands._bound_task_record({"name": "task", "dir": "unused"}, identity)["status"] == "completed"


@pytest.mark.parametrize("status", ["queued", "running"])
def test_old_lease_snapshot_does_not_refresh_same_owner_replacement(tmp_path, monkeypatch, status):
    manager, task_dir = _manager(tmp_path, monkeypatch)
    assert manager._sync_status_to_disk("task", status, expected_statuses={"pending"})

    def replace_before_update(path, updater, **kwargs):
        def replace(info):
            info["_attempt_id"] = "new-admission"
            info["lease_until"] = 17.0
        update_task_metadata(path, replace)
        return update_task_metadata(path, updater, **kwargs)

    monkeypatch.setattr(manager_module, "update_task_metadata", replace_before_update)
    manager._refresh_queued_runner_leases()
    assert load_task_metadata(str(task_dir))["lease_until"] == 17.0


def test_old_worker_does_not_consume_replacement_stop_request(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    save_task_info(str(task_dir), {
        "status": "running", "run_index": 1, "_attempt_id": "replacement",
        "_pending_stop_summary": {"run_index": 1, "reason": "cancelled_by_user"},
    })
    assert executor._read_pending_stop_summary(
        str(task_dir), 1, expected_attempt_id="old-worker",
    ) is None
    assert load_task_metadata(str(task_dir))["_pending_stop_summary"]["reason"] == "cancelled_by_user"


def test_failed_admission_keeps_identity_before_worker_claim(tmp_path, monkeypatch):
    manager, task_dir = _manager(tmp_path, monkeypatch)
    assert manager._sync_status_to_disk("task", "running", expected_statuses={"pending"})
    admitted = load_task_metadata(str(task_dir))
    attempt_id = admitted["_attempt_id"]
    assert admitted["run_attempt_ids"] == [attempt_id]
    manager._mark_failed_on_disk(manager.get_task("task"), final_status="failed")
    assert manager._sync_status_to_disk("task", "running", run_index=2, expected_statuses={"failed"})
    assert detached_runner._submitted_run_status(
        load_task_metadata(str(task_dir)), 1, expected_runner_id=manager.runner_id,
        expected_attempt_id=attempt_id,
    ) == "failed"


def test_old_source_collector_cannot_append_to_replacement_log(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    save_task_info(str(task_dir), {
        "status": "completed", "run_index": 1, "_attempt_id": "replacement",
        "run_attempt_ids": ["replacement"],
    })
    executor._persist_run_source_state(
        task_dir=str(task_dir), task_name="task", log_path="unused", run_index=1,
        source_state="old git state", expected_attempt_id="old-task",
    )
    assert not (task_dir / RUN_LOGS_DIR / "run1.log").exists()
    assert load_task_metadata(str(task_dir))["source_states"] == [""]


def test_capture_after_unstarted_rerun_cancel_keeps_cancelled_admission(monkeypatch):
    info = {
        "status": "cancelled", "run_index": 1, "run_statuses": ["completed"],
        "_attempt_id": "cancelled-queue", "run_attempt_ids": ["completed-run"],
    }
    task = {"name": "task", "dir": "unused"}
    monkeypatch.setattr(commands, "load_task_metadata", lambda _: info)
    monkeypatch.setattr(commands, "_selected_run_record", lambda *_a, **_k: {"status": "completed"})
    monkeypatch.setattr(commands, "_task_record", lambda *_a, **_k: {})
    identity = commands._capture_task_run_identity(task)
    assert identity.run_index == 2
    assert identity.started_queued
    assert identity.attempt_id == "cancelled-queue"
    assert commands._bound_task_record(task, identity)["status"] == "cancelled"
