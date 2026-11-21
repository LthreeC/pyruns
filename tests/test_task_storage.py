"""Git replicas expose common history and this device's store without claiming remote work."""

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyruns.cli.commands import cmd_restore
from pyruns.core.task_generator import TaskGenerator
from pyruns.core.task_manager import TaskManager
from pyruns.utils import task_storage
from pyruns.utils.info_io import update_task_metadata
from pyruns.utils.runner_device import local_runner_host
from pyruns.utils.run_result import write_run_result
from pyruns.web.runtime import PyrunsRuntime


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
         "-c", "core.autocrlf=false", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.mark.parametrize("old_device", [None, "server-device"])
def test_git_pull_never_claims_another_devices_tasks(tmp_path, monkeypatch, old_device):
    monkeypatch.setattr(TaskManager, "_scheduler_loop", lambda self: None)
    source = tmp_path / "server"
    source.mkdir()
    git(source, "init")
    root = source / "workspace" / "tasks"
    common = TaskGenerator(str(root))
    for name in ("snapshot", "queue", "no-owner", "with-result"):
        task = common.create_task(name, {"x": 1})
        status = "queued" if name in {"queue", "no-owner"} else "running"
        update_task_metadata(task["dir"], lambda info: info.update(
            status=status, run_index=int(status == "running"),
            runner_id=None if name == "no-owner" else "other:123:token",
            runner_host=None if name == "no-owner" else local_runner_host(),
            runner_device=old_device if name != "no-owner" else None,
            lease_until=time.time() - 30, pids=[os.getpid()], pid_create_times=[1.0],
        ))
    write_run_result(str(root / "with-result"), dict(
        run_index=1, runner_id="other:123:token", attempt_id="", status="completed",
        finish_time="2026-11-21_12-00-00", exit_code=0,
    ))
    monkeypatch.setattr(task_storage, "device_tasks_folder", lambda: "tasks-server-111")
    TaskGenerator(task_storage.new_task_root(str(root), "local")).create_task("same-name", {"source": 1})
    git(source, "add", "workspace")
    git(source, "commit", "-m", "server snapshot")
    checkout = tmp_path / "laptop"
    git(tmp_path, "clone", "--local", str(source), str(checkout))
    monkeypatch.setattr(task_storage, "device_tasks_folder", lambda: "tasks-laptop-222")
    laptop = checkout / "workspace" / "tasks"
    manager = TaskManager(str(laptop), lazy_scan=False)
    try:
        assert {t["name"] for t in manager.list_tasks()} == {"snapshot", "queue", "no-owner", "with-result"}
        assert manager.get_task("with-result")["status"] == "completed"
        assert manager.get_task("snapshot")["runner_location"] == ("remote" if old_device else "unknown")
        manager.refresh_from_disk(force_all=True)
        manager._refresh_queued_runner_leases()
        assert manager._pick_queued_task()[0] is None
        assert manager._claim_task_for_run(manager.get_task("queue"), 1, counts_for_batch=True) is None
        assert not manager.request_task_cancel("snapshot")
        assert not manager.is_processing
        assert manager.local_activity_counts() == {"running": 0, "queued": 0}
        manager._cleanup_on_shutdown()
        assert not git(checkout, "status", "--porcelain", "--untracked-files=no")

        local = TaskGenerator(task_storage.new_task_root(str(laptop), "local"))
        local.create_task("same-name", {"source": 2})
        git(checkout, "add", "workspace/tasks-laptop-222")
        git(checkout, "commit", "-m", "local task with the same name")
        # The source also adds a common task with this name after the clone.
        TaskGenerator(str(root)).create_task("same-name", {"source": 3}, exact_name=True)
        update_task_metadata(str(root / "snapshot"), lambda info: info.update(status="completed", exit_codes=[0]))
        git(source, "add", "workspace")
        git(source, "commit", "-m", "server finished")
        git(checkout, "pull", "--no-rebase", "--no-edit")
        manager.refresh_from_disk(discover=True, force_all=True)
        assert manager.get_task("snapshot")["status"] == "completed"
        assert manager.get_task("same-name")["config"]["source"] == 2
        assert manager.get_task("same-name (shared)")["config"]["source"] == 3
        assert not git(checkout, "status", "--porcelain", "--untracked-files=no")
    finally:
        manager.shutdown()


def test_storage_switch_preserves_history_and_trash_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(TaskManager, "_scheduler_loop", lambda self: None)
    runtime = PyrunsRuntime(str(tmp_path))
    try:
        common = runtime.task_generator.create_task("old", {"lr": 1})
        assert Path(common["dir"]).parent == tmp_path / "tasks"
        runtime.update_runtime_settings({"task_storage": "local"})
        private = runtime.task_generator.create_task("new", {"lr": 2})
        local_root = tmp_path / task_storage.device_tasks_folder()
        assert Path(private["dir"]).parent == local_root
        runtime.update_runtime_settings({"task_storage": "shared"})
        assert Path(runtime.task_generator.root_dir) == tmp_path / "tasks"
        manager = runtime.task_manager
        manager.scan_disk()
        assert {task["name"] for task in manager.list_tasks()} == {"old", "new"}
        assert manager.rename_task("new", "renamed") == (True, "renamed")
        assert (local_root / "renamed").is_dir()
        assert manager.delete_tasks(["old", "renamed"]) == ["old", "renamed"]
        assert (local_root / ".trash" / "renamed").is_dir()
        cmd_restore(SimpleNamespace(json_output=True), SimpleNamespace(tasks=["old", "renamed"]), manager)
        assert Path(manager.get_task("renamed")["dir"]).parent == local_root
        assert Path(manager.get_task("old")["dir"]).parent == tmp_path / "tasks"
        assert manager._sync_status_to_disk("renamed", "queued", run_index=1, expected_statuses={"pending"})
        assert manager.local_activity_counts() == {"running": 0, "queued": 1}
    finally:
        runtime.shutdown()


def test_git_tracking_between_queue_selection_and_claim_is_rechecked(tmp_path, monkeypatch):
    monkeypatch.setattr(TaskManager, "_scheduler_loop", lambda self: None)
    git(tmp_path, "init")
    tasks = tmp_path / "tasks"
    task = TaskGenerator(str(tasks)).create_task("legacy", {})
    update_task_metadata(task["dir"], lambda info: info.update(status="queued"))
    manager = TaskManager(str(tasks), lazy_scan=False)
    try:
        candidate, index = manager._pick_queued_task()
        assert candidate and candidate["name"] == "legacy"
        git(tmp_path, "add", "tasks/legacy/task_info.json")
        assert manager._claim_task_for_run(candidate, index, counts_for_batch=True) is None
        assert not git(tmp_path, "diff", "--name-only")
    finally:
        manager.shutdown()
