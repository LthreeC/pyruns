"""Search metadata must stay fresh without retaining removed task payloads."""
import gc
import weakref

import pytest

from pyruns.core.task_manager import TaskManager


@pytest.fixture
def manager(tmp_path):
    instance = TaskManager(str(tmp_path / "tasks"), lazy_scan=None, owns_task_lifecycle=False)
    with instance._lock:
        instance.tasks = [{
            "dir": str(tmp_path / "tasks" / "source"), "name": "source", "status": "pending",
            "pinned": False, "task_order": 1, "created_at": "2026-01-01_00-00-00",
            "start_times": ["old", "2026-01-01_00-00-01"],
            "finish_times": ["old", "2026-01-01_00-00-02"],
            "notes": "original", "env": {"A": "one", "B": "two"},
            "search_text": "source original", "task_kind": "config",
            "config": {"tag": "original"}, "config_text": "",
        }]
        instance._rebuild_indexes_locked()
    try:
        yield instance
    finally:
        instance.shutdown()


def materialize(view):
    return {**view, "start_times": list(view["start_times"]),
            "finish_times": list(view["finish_times"]), "env": dict(view["env"])}


@pytest.mark.parametrize("field,value", [
    ("dir", "changed/directory"), ("name", "renamed"), ("status", "running"),
    ("pinned", True), ("task_order", -2), ("created_at", "2026-02-01_00-00-00"),
    ("start_times", ["2026-02-01_00-00-01"]), ("finish_times", []),
    ("notes", "changed"), ("env", {"A": "changed"}), ("search_text", "changed text"),
    ("task_kind", "shell"), ("config", {"tag": "changed"}), ("config_text", "echo changed"),
])
def test_search_views_capture_each_changed_source_without_rewriting_old_views(manager, field, value):
    old = manager._get_task_search_views()["source"]
    expected_old = manager.get_task_search_snapshots()["source"]
    with manager._lock:
        manager.tasks[0][field] = value
        manager._rebuild_indexes_locked()
    name = "renamed" if field == "name" else "source"
    current = manager._get_task_search_views()[name]
    assert materialize(current) == manager.get_task_search_snapshots()[name]
    assert materialize(old) == expected_old
    assert current is not old


def test_search_views_detect_in_place_changes_and_environment_order(manager):
    old = manager._get_task_search_views()["source"]
    with manager._lock:
        task = manager.tasks[0]
        task["start_times"][-1] = "updated-start"
        task["finish_times"].append("updated-finish")
        task["env"]["A"] = "changed"
    updated = manager._get_task_search_views()["source"]
    assert updated["start_times"] == ("updated-start",)
    assert updated["finish_times"] == ("updated-finish",)
    assert updated["env"]["A"] == "changed"
    assert old["env"]["A"] == "one"
    assert old["start_times"] == ("2026-01-01_00-00-01",)
    with manager._lock:
        task["env"]["A"] = task["env"].pop("A")
    reordered = manager._get_task_search_views()["source"]
    assert list(reordered["env"]) == ["B", "A"]
    assert list(updated["env"]) == ["A", "B"]
    assert reordered is not updated


@pytest.mark.parametrize("field,before,after", [
    ("notes", 1, True), ("created_at", 1, 1.0),
    ("start_times", [1], [True]), ("finish_times", [1], [1.0]),
    ("env", {"A": 1}, {"A": True}),
])
def test_search_views_distinguish_equal_values_with_different_text(manager, field, before, after):
    with manager._lock:
        manager.tasks[0][field] = before
    old = manager._get_task_search_views()["source"]
    with manager._lock:
        manager.tasks[0][field] = after
    current = manager._get_task_search_views()["source"]
    assert str(materialize(old)[field]) == str(before)
    assert str(materialize(current)[field]) == str(after)


def test_search_views_keep_public_snapshot_isolation_and_config_reference_contract(manager):
    views = manager._get_task_search_views()
    old = views.pop("source")
    assert manager._get_task_search_views()["source"] is old
    with pytest.raises(TypeError):
        old["notes"] = "mutated"
    with pytest.raises(TypeError):
        old["env"]["A"] = "mutated"
    snapshot = manager.get_task_search_snapshots(["source", "absent"])
    assert list(snapshot) == ["source"]
    snapshot["source"]["notes"] = "mutated"
    snapshot["source"]["env"]["A"] = "mutated"
    snapshot["source"]["start_times"].clear()
    assert manager.get_task_search_snapshots()["source"] == materialize(old)
    assert snapshot["source"]["config"] is old["config"] is manager.tasks[0]["config"]
    with manager._lock:
        manager.tasks[0]["config"] = dict(old["config"])
    replaced = manager._get_task_search_views()["source"]
    assert replaced["config"] == old["config"]
    assert replaced["config"] is not old["config"]
    assert replaced is not old


@pytest.mark.parametrize("empty", [{}, {"env": None, "start_times": None, "finish_times": ()}])
def test_search_view_missing_values_keep_public_defaults(manager, empty):
    with manager._lock:
        manager.tasks = [{"name": "minimal", **empty}]
        manager._rebuild_indexes_locked()
    view = manager._get_task_search_views()["minimal"]
    assert materialize(view) == manager.get_task_search_snapshots()["minimal"]
    assert manager._get_task_search_views()["minimal"] is view


def test_search_cache_releases_deleted_payload_after_last_reader_finishes(manager):
    class Config(dict):
        pass

    with manager._lock:
        manager.tasks[0]["config"] = Config(tag="large-payload")
    captured = manager._get_task_search_views()
    reference = weakref.ref(captured["source"]["config"])
    with manager._lock:
        manager.tasks.clear()
        manager._rebuild_indexes_locked()
    assert not manager._search_view_cache
    assert not manager._get_task_search_views()
    assert reference() is not None  # A running request still owns its snapshot.
    del captured
    gc.collect()
    assert reference() is None


def test_search_cache_clears_on_shutdown_and_cannot_be_refilled(manager):
    captured = manager._get_task_search_views()
    manager.shutdown()
    assert not manager._search_view_cache
    assert materialize(captured["source"]) == manager.get_task_search_snapshots()["source"]
    assert manager._get_task_search_views()
    assert not manager._search_view_cache
