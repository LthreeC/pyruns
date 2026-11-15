"""Task page indexes stay fresh while avoiding repeated workspace scans."""

from unittest.mock import patch

import pytest

import pyruns.core.task_manager as task_manager_module
from pyruns._config import CONFIG_FILENAME, TASK_KIND_CONFIG
from pyruns.core.gpu_scheduler import GpuSchedulerConfig
from pyruns.core.task_manager import TaskManager
from pyruns.utils.info_io import load_task_metadata, save_task_info, update_task_metadata
from pyruns.utils.sort_utils import sort_tasks_for_manager


@pytest.fixture
def manager(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    for index, name in enumerate(("task10", "task2", "task3")):
        task_dir = tasks_dir / name
        task_dir.mkdir()
        (task_dir / CONFIG_FILENAME).write_text(f"value: {index}\n", encoding="utf-8")
        save_task_info(str(task_dir), {
            "name": name, "status": "pending", "created_at": f"2026-10-10_00-00-0{index}",
            "task_kind": TASK_KIND_CONFIG, "config_file": CONFIG_FILENAME,
            "notes": "before", "env": {"TOKEN": "before"},
        })
    instance = TaskManager(str(tasks_dir), lazy_scan=False, owns_task_lifecycle=False)
    yield instance
    instance.shutdown()


def names(page):
    return [task["name"] for task in page[0]]


@pytest.mark.parametrize("warm_index", [False, True])
def test_active_group_filters_cached_and_cold_pages_without_changing_global_totals(manager, warm_index):
    from pyruns.utils.sort_utils import filter_tasks

    with manager._lock:
        for task, status in zip(manager.tasks, ("running", "queued", "completed")):
            task["status"] = status
        manager._rebuild_indexes_locked()
    expected = {task["name"] for task in manager.tasks if task["status"] in {"running", "queued"}}
    if warm_index:
        manager.get_task_page(status="All", summary=True)
    page = manager.get_task_page(status="active", limit=1, summary=True)
    second = manager.get_task_page(status="active", offset=1, limit=1, summary=True)
    assert set(names(page) + names(second)) == expected
    assert page[1] == second[1] == 2
    assert page[2] == {
        "pending": 0, "queued": 1, "running": 1,
        "completed": 1, "failed": 0, "cancelled": 0,
    }
    selected_name = next(iter(expected))
    assert names(manager.get_task_page(status="active", query=selected_name, search_field="name", summary=True)) == [selected_name]
    assert {task["name"] for task in filter_tasks(manager.tasks, "", "Active")} == expected


def test_repeated_pages_share_sort_counts_and_copy_only_selected_tasks(manager):
    class CountedTasks(list):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    manager.tasks = CountedTasks(manager.tasks)
    with manager._lock:
        manager._rebuild_indexes_locked()
    with (
        patch.object(task_manager_module, "sort_tasks_for_manager", wraps=sort_tasks_for_manager) as sort,
        patch.object(manager, "_snapshot_task_for_api", wraps=manager._snapshot_task_for_api) as snapshot,
    ):
        first = manager.get_task_page(sort_mode="name_asc", limit=1, summary=True)
        baseline_iterations = manager.tasks.iterations
        first[2]["pending"] = 999
        first[0][0]["env"]["TOKEN"] = "detached"
        second = manager.get_task_page(sort_mode="name_asc", offset=1, limit=1, compact=True, summary=True)
        third = manager.get_task_page(sort_mode="name_asc", offset=2, limit=1)
        assert sort.call_count == 1
        assert snapshot.call_count == 3
        assert manager.tasks.iterations == baseline_iterations
    assert names(first) + names(second) + names(third) == ["task2", "task3", "task10"]
    assert second[1] == third[1] == 3
    assert second[2]["pending"] == third[2]["pending"] == 3
    assert manager.get_task("task2", summary=True)["env"]["TOKEN"] == "before"


def test_search_reads_current_notes_env_and_payload_without_resorting(manager):
    with patch.object(task_manager_module, "sort_tasks_for_manager", wraps=sort_tasks_for_manager) as sort:
        manager.get_task_page(sort_mode="name_asc", summary=True)
        assert manager.get_task_page(query="after", search_field="notes", sort_mode="name_asc")[1] == 0
        assert manager.update_task_notes("task2", "after", "before")[0]
        assert names(manager.get_task_page(query="after", search_field="notes", sort_mode="name_asc")) == ["task2"]
        assert manager.update_task_env("task3", {"TOKEN": "after"}, {"TOKEN": "before"})[0]
        assert names(manager.get_task_page(query="TOKEN=after", search_field="env", sort_mode="name_asc")) == ["task3"]
        manager._tasks_by_name["task10"]["config"]["value"] = 55
        assert names(manager.get_task_page(query="value: 55", search_field="config", sort_mode="name_asc")) == ["task10"]
        manager._tasks_by_name["task10"]["config"]["value"] = 56
        assert manager.get_task_page(query="value: 55", search_field="config", sort_mode="name_asc")[1] == 0
        assert sort.call_count == 1


def test_cold_search_sorts_matches_only_and_shares_live_workspace_counts(manager):
    with patch.object(task_manager_module, "sort_tasks_for_manager", wraps=sort_tasks_for_manager) as sort:
        page = manager.get_task_page(query="task2", search_field="name", sort_mode="name_asc")
        assert names(page) == ["task2"]
        assert len(sort.call_args.args[0]) == 1
        assert page[2]["pending"] == 3
        assert manager._task_page_cache == {}


def test_first_status_filter_sorts_its_candidates_only_and_reuses_other_views(manager):
    task = manager._tasks_by_name["task2"]
    updated = update_task_metadata(task["dir"], lambda info: info.update(status="failed"))
    with manager._lock:
        manager._apply_info_to_task(task, updated)
    with patch.object(task_manager_module, "sort_tasks_for_manager", wraps=sort_tasks_for_manager) as sort:
        assert names(manager.get_task_page(status="failed")) == ["task2"]
        assert len(sort.call_args.args[0]) == 1
        assert len(manager.get_task_page()[0]) == 3
        assert len(sort.call_args.args[0]) == 3
        assert manager.get_task_page(status="pending")[1] == 2
        assert manager.get_task_page(status="FAILED")[1] == 1
        assert sort.call_count == 2


def test_pin_and_manual_order_invalidate_cached_pages(manager):
    with patch.object(task_manager_module, "sort_tasks_for_manager", wraps=sort_tasks_for_manager) as sort:
        assert names(manager.get_task_page(sort_mode="manual")) == ["task3", "task2", "task10"]
        assert manager.set_task_pinned("task10", True)[0]
        assert names(manager.get_task_page(sort_mode="manual"))[0] == "task10"
        assert manager.reorder_tasks([
            {"name": "task2", "pinned": True}, {"name": "task10", "pinned": True},
        ])[0]
        assert names(manager.get_task_page(sort_mode="manual")) == ["task2", "task10", "task3"]
        assert sort.call_count == 3


def test_queue_dispatch_and_terminal_update_refresh_counts_and_status_filter(manager, monkeypatch):
    monkeypatch.setattr(manager, "_gpu_scheduler_config", lambda: GpuSchedulerConfig(enabled=False))
    with patch.object(task_manager_module, "sort_tasks_for_manager", wraps=sort_tasks_for_manager) as sort:
        assert manager.get_task_page(status="QUEUED")[1] == 0
        assert manager._sync_status_to_disk("task2", "queued", expected_statuses={"pending"})
        assert names(manager.get_task_page(status="queued")) == ["task2"]
        task, run_index = manager._pick_queued_task()
        assert task["name"] == "task2"
        assert run_index == 1
        running = manager.get_task_page(status="running")
        assert names(running) == ["task2"]
        assert running[2]["queued"] == 0
        assert running[2]["running"] == 1
        manager._mark_failed_on_disk(task, expected_statuses={"queued"}, final_status="cancelled")
        terminal = manager.get_task_page(status="cancelled")
        assert names(terminal) == ["task2"]
        assert terminal[2]["running"] == 0
        assert sort.call_count == 4


def test_progress_heartbeat_and_metrics_preserve_sort_cache_but_payload_is_fresh(manager):
    with patch.object(task_manager_module, "sort_tasks_for_manager", wraps=sort_tasks_for_manager) as sort:
        manager.get_task_page(sort_mode="name_asc")
        task = manager._tasks_by_name["task2"]

        def heartbeat(info):
            info["progress"] = 0.75
            info["lease_heartbeat"] = 123.0
            info["records"] = [{"loss": 0.5}]

        updated = update_task_metadata(task["dir"], heartbeat)
        with manager._lock:
            manager._apply_info_to_task(task, updated)
        page = manager.get_task_page(sort_mode="name_asc")
        assert page[0][0]["progress"] == 0.75
        assert page[0][0]["records"] == [{"loss": 0.5}]
        assert sort.call_count == 1


@pytest.mark.parametrize("field,value", [
    ("start_times", ["2026-10-11_00-00-01"]),
    ("finish_times", ["2026-10-11_00-00-02"]),
    ("status", "failed"),
])
def test_disk_refresh_invalidates_activity_and_status_changes(manager, field, value):
    with patch.object(task_manager_module, "sort_tasks_for_manager", wraps=sort_tasks_for_manager) as sort:
        manager.get_task_page(sort_mode="activity_desc")
        task = manager._tasks_by_name["task10"]
        update_task_metadata(task["dir"], lambda info: info.update({field: value}))
        assert manager.refresh_from_disk(task_ids=["task10"])
        page = manager.get_task_page(sort_mode="activity_desc")
        if field != "status":
            assert names(page)[0] == "task10"
        else:
            assert page[2]["failed"] == 1
        assert sort.call_count == 2


def test_registry_rename_remove_and_reload_invalidate_cached_pages(manager):
    with patch.object(task_manager_module, "sort_tasks_for_manager", wraps=sort_tasks_for_manager) as sort:
        manager.get_task_page(sort_mode="name_asc")
        assert manager.rename_task("task2", "task1")[0]
        assert names(manager.get_task_page(sort_mode="name_asc"))[0] == "task1"
        assert manager.delete_tasks(["task1"]) == ["task1"]
        assert manager.get_task_page(sort_mode="name_asc")[1] == 2
        task = manager._tasks_by_name["task10"]
        update_task_metadata(task["dir"], lambda info: info.update(status="completed"))
        manager.load_task_by_name("task10")
        assert manager.get_task_page(sort_mode="name_asc")[2]["completed"] == 1
        manager.add_task({"name": "added", "status": "pending", "config": {}})
        assert names(manager.get_task_page(sort_mode="name_asc"))[0] == "added"
        assert sort.call_count == 5


def test_cache_is_bounded_and_source_replacement_and_invalid_sort_stay_correct(manager):
    for status in ["All", "PENDING", "pending", *(f"unknown{index}" for index in range(30))]:
        manager.get_task_page(sort_mode="name_asc", status=status)
        assert len(manager._task_page_cache) <= task_manager_module._TASK_PAGE_CACHE_SIZE
    with pytest.raises(ValueError, match="Unknown task sort mode"):
        manager.get_task_page(sort_mode="broken")
    manager.tasks = [{"name": "replacement", "status": "failed"}]
    assert names(manager.get_task_page(sort_mode="name_asc")) == ["replacement"]
    manager.tasks.append({"name": "appended", "status": "completed"})
    assert names(manager.get_task_page(sort_mode="name_asc")) == ["appended", "replacement"]


def test_detached_metadata_application_does_not_discard_live_index(manager):
    manager.get_task_page(sort_mode="name_asc")
    task = manager._tasks_by_name["task2"]
    with patch.object(task_manager_module, "sort_tasks_for_manager", wraps=sort_tasks_for_manager) as sort:
        info = load_task_metadata(task["dir"])
        info["status"] = "failed"
        manager._apply_info_to_task(dict(task), info)
        assert manager.get_task_page(sort_mode="name_asc")[2]["failed"] == 0
        assert sort.call_count == 0


@pytest.mark.parametrize("operation", ["terminal", "pending_stop"])
@pytest.mark.parametrize("interleaving", ["registry", "disk_only", "replacement"])
def test_stop_publication_preserves_newer_run_or_completion(manager, monkeypatch, operation, interleaving):
    task = manager._tasks_by_name["task2"]
    original_update = update_task_metadata
    seeded = original_update(task["dir"], lambda info: info.update({
        "status": "running", "run_index": 1, "start_times": ["2026-10-15_12-00-00"],
    }))
    with manager._lock:
        manager._apply_info_to_task(task, seeded)
    manager.get_task_page()
    new_status = "running" if operation == "terminal" else "completed"
    new_run_index = 2 if operation == "terminal" else 1

    def advance():
        latest = original_update(task["dir"], lambda info: info.update({
            "status": new_status, "run_index": new_run_index,
            "start_times": ["2026-10-15_12-00-00", "2026-10-15_12-01-00"]
            if operation == "terminal" else ["2026-10-15_12-00-00"],
            "finish_times": ["", ""] if operation == "terminal" else ["2026-10-15_12-01-00"],
        }))
        with manager._lock:
            if interleaving == "registry":
                manager._apply_info_to_task(task, latest)
            elif interleaving == "replacement":
                replacement = dict(task)
                manager._apply_info_to_task(replacement, latest)
                replacement["_registry_revision"] = task["_registry_revision"]
                manager.tasks = [replacement if current is task else current for current in manager.tasks]
                manager._rebuild_indexes_locked()

    if operation == "terminal":
        monkeypatch.setattr(manager, "_append_error_summary", lambda *args, **kwargs: advance())
        manager._mark_failed_on_disk(task, reason="controlled stop", expected_statuses={"running"})
    else:
        def advance_after_write(*args, **kwargs):
            updated = original_update(*args, **kwargs)
            advance()
            return updated

        monkeypatch.setattr(task_manager_module, "update_task_metadata", advance_after_write)
        manager._persist_pending_stop_summary(
            task, event="stopped", reason="controlled stop", expected_statuses={"running"},
        )
    persisted = load_task_metadata(task["dir"])
    current = manager.get_task("task2", summary=True)
    assert persisted["status"] == current["status"] == new_status
    assert persisted["run_index"] == current["run_index"] == new_run_index
    page = manager.get_task_page(status=new_status)
    assert names(page) == ["task2"]
    assert page[2][new_status] == 1
    if interleaving == "replacement":
        assert task["status"] == "running"
        assert task["run_index"] == 1
        assert task.get("cancel_requested_at") is None


@pytest.mark.parametrize("operation", ["terminal", "pending_stop"])
@pytest.mark.parametrize("detached", [False, True])
def test_stop_publication_updates_normal_live_and_detached_inputs(manager, operation, detached):
    live = manager._tasks_by_name["task2"]
    seeded = update_task_metadata(live["dir"], lambda info: info.update(status="running", run_index=1))
    with manager._lock:
        manager._apply_info_to_task(live, seeded)
    task = dict(live) if detached else live
    if operation == "terminal":
        manager._mark_failed_on_disk(task, expected_statuses={"running"})
        assert task["status"] == "failed"
    else:
        manager._persist_pending_stop_summary(task, event="stopped", reason="requested", expected_statuses={"running"})
        assert task["cancel_requested_at"]
    if detached:
        assert live["status"] == "running"
        assert live.get("cancel_requested_at") is None
    else:
        assert manager.get_task("task2", summary=True)["status"] == task["status"]


@pytest.mark.parametrize("operation", ["terminal", "pending_stop"])
@pytest.mark.parametrize("detached", [False, True])
def test_stop_publication_read_failure_keeps_live_state_and_detached_result(manager, operation, detached):
    live = manager._tasks_by_name["task2"]
    seeded = update_task_metadata(live["dir"], lambda info: info.update(status="running", run_index=1))
    with manager._lock:
        manager._apply_info_to_task(live, seeded)
    task = dict(live) if detached else live
    with patch.object(task_manager_module, "load_task_metadata", side_effect=OSError("temporarily unavailable")):
        if operation == "terminal":
            manager._mark_failed_on_disk(task, expected_statuses={"running"})
        else:
            manager._persist_pending_stop_summary(task, event="stopped", reason="requested", expected_statuses={"running"})
    assert live["status"] == "running"
    assert live.get("cancel_requested_at") is None
    if detached:
        if operation == "terminal":
            assert task["status"] == "failed"
        else:
            assert task["cancel_requested_at"]
    assert manager.refresh_from_disk(task_ids=["task2"])
    if operation == "terminal":
        assert manager.get_task("task2", summary=True)["status"] == "failed"
    else:
        assert manager.get_task("task2", summary=True)["status"] == "running"
        assert manager.get_task("task2")["cancel_requested_at"]
