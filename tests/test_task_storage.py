"""Git replicas expose common history and this device's store without claiming remote work."""

import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyruns.cli.commands import _trash_records, cmd_restore
from pyruns.core.task_generator import TaskGenerator
from pyruns.core.task_manager import TaskManager
from pyruns.utils import runner_device, task_storage
from pyruns.utils.info_io import load_task_metadata, update_task_metadata
from pyruns.utils.runner_device import local_runner_host
from pyruns.utils.run_result import write_run_result
from pyruns.web.runtime import PyrunsRuntime


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
         "-c", "core.autocrlf=false", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_linux_runner_without_machine_id_uses_boot_and_pid_namespace(monkeypatch):
    monkeypatch.setattr(runner_device.sys, "platform", "linux")
    monkeypatch.setattr(runner_device, "_machine_fingerprint", lambda: None)
    boot = "63d08101-c11b-4946-bfd6-7e31558049dc"
    namespace = "pid:[4026531836]"
    monkeypatch.setattr(runner_device.Path, "read_text", lambda self, **kwargs: boot)
    monkeypatch.setattr(runner_device.os, "readlink", lambda path: namespace)
    fingerprint = runner_device.local_runner_device.__wrapped__()
    assert fingerprint
    assert runner_device.local_runner_device.__wrapped__() == fingerprint
    boot = "8cadc4d5-f199-4299-bc08-210b40a02250"
    assert runner_device.local_runner_device.__wrapped__() not in (None, fingerprint)
    boot = "63d08101-c11b-4946-bfd6-7e31558049dc"
    namespace = "pid:[4026532345]"
    assert runner_device.local_runner_device.__wrapped__() not in (None, fingerprint)
    boot = ""
    assert runner_device.local_runner_device.__wrapped__() is None


@pytest.mark.parametrize("old_device", [None, "server-device"])
def test_git_pull_never_claims_another_devices_tasks(tmp_path, monkeypatch, old_device):
    monkeypatch.setattr(TaskManager, "_scheduler_loop", lambda self: None)
    with monkeypatch.context() as hardware:
        hardware.setattr(runner_device, "local_runner_host", lambda: "worker")
        hardware.setattr(runner_device, "_machine_fingerprint", lambda: "1" * 64)
        source_store = runner_device.device_tasks_folder.__wrapped__()
        hardware.setattr(runner_device, "_machine_fingerprint", lambda: "2" * 64)
        checkout_store = runner_device.device_tasks_folder.__wrapped__()
    assert source_store == "tasks-worker-111111111111"
    assert checkout_store == "tasks-worker-222222222222"
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
    monkeypatch.setattr(task_storage, "device_tasks_folder", lambda: source_store)
    TaskGenerator(task_storage.new_task_root(str(root), "local")).create_task("same-name", {"source": 1})
    git(source, "add", "workspace")
    git(source, "commit", "-m", "server snapshot")
    checkout = tmp_path / "laptop"
    git(tmp_path, "clone", "--local", str(source), str(checkout))
    monkeypatch.setattr(task_storage, "device_tasks_folder", lambda: checkout_store)
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
        assert not manager.is_processing
        assert manager.local_activity_counts() == {"running": 0, "queued": 0}
        manager._cleanup_on_shutdown()
        assert not git(checkout, "status", "--porcelain", "--untracked-files=no")

        local = TaskGenerator(task_storage.new_task_root(str(laptop), "local"))
        local.create_task("same-name", {"source": 2})
        git(checkout, "add", f"workspace/{checkout_store}")
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
        assert (checkout / "workspace" / source_store / "same-name").is_dir()
        assert (checkout / "workspace" / checkout_store / "same-name").is_dir()
        assert not git(checkout, "status", "--porcelain", "--untracked-files=no")
    finally:
        manager.shutdown()


def test_same_device_uses_the_same_task_store_in_another_git_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(TaskManager, "_scheduler_loop", lambda self: None)
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init")
    monkeypatch.chdir(source)
    tasks = source / "workspace" / "tasks"
    TaskGenerator(str(tasks)).create_task("legacy", {})
    local = Path(task_storage.new_task_root(str(tasks), "local"))
    TaskGenerator(str(local)).create_task("trial", {})
    git(source, "add", "workspace")
    git(source, "commit", "-m", "local tasks")
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", "--local", str(source), str(checkout))
    monkeypatch.chdir(checkout)
    cloned_tasks = checkout / "workspace" / "tasks"
    cloned_local = Path(task_storage.new_task_root(str(cloned_tasks), "local"))
    assert cloned_local.relative_to(checkout) == local.relative_to(source)
    manager = TaskManager(str(cloned_tasks), lazy_scan=False)
    try:
        assert manager.get_task("legacy", summary=True)["task_source"] == "shared"
        assert manager.get_task("trial", summary=True)["task_source"] == "local"
        assert Path(manager.task_directory("trial")) == cloned_local / "trial"
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


@pytest.mark.parametrize('known_runner', [True, False])
def test_git_tracked_common_task_accepts_explicit_stop_from_another_host(tmp_path, monkeypatch, known_runner):
    monkeypatch.setattr(TaskManager, "_scheduler_loop", lambda self: None)
    monkeypatch.setattr("pyruns.core.task_manager.kill_process", lambda *_args, **_kwargs: pytest.fail("foreign PID"))
    git(tmp_path, "init")
    root = tmp_path / "tasks"
    task_dir = TaskGenerator(str(root)).create_task("shared-job", {})["dir"]
    runner_host = f"peer-{local_runner_host()}"
    runner_id = f"{runner_host}:123:token" if known_runner else None
    update_task_metadata(task_dir, lambda info: info.update(
        status="running", run_index=1, runner_host=runner_host if known_runner else None, runner_id=runner_id,
        lease_until=time.time() - 30, pids=[os.getpid()], pid_create_times=[1.0],
    ))
    git(tmp_path, "add", "tasks/shared-job/task_info.json")
    manager = TaskManager(str(root), lazy_scan=False)
    try:
        task = manager.get_task("shared-job")
        assert task["task_storage"] == "shared"
        assert task["runner_location"] == ("remote" if known_runner else "unknown")
        assert manager.request_task_cancel("shared-job", expected_runner_id=runner_id, expected_run_index=1)
        info = load_task_metadata(task_dir)
        assert info["cancel_requested_at"]
        assert info["status"] == "running"
        assert info["runner_id"] == runner_id
    finally:
        manager.shutdown()


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


def test_web_observes_other_stores_without_claiming_them(tmp_path, monkeypatch):
    monkeypatch.setattr(TaskManager, "_scheduler_loop", lambda self: None)
    runtime = PyrunsRuntime(str(tmp_path))
    try:
        shared = runtime.task_generator.create_task("trial", {})
        remote = "tasks-render-farm-node-aaaaaaaaaaaa"
        generator = TaskGenerator(str(tmp_path / remote))
        replica = Path(generator.create_task("trial", {})["dir"])
        queued = generator.create_task("other", {})
        update_task_metadata(queued["dir"], lambda info: info.update(status="queued"))
        update_task_metadata(str(replica), lambda info: info.update(
            status="running", run_index=1, runner_host=local_runner_host(),
            runner_id="remote-runner", lease_until=time.time() + 3600, pids=[os.getpid()],
        ))
        logs = replica / "run_logs"
        logs.mkdir(exist_ok=True)
        (logs / "run1.log").write_text("device-specific-output\n", encoding="utf-8")
        before = (replica / "task_info.json").read_bytes()
        (tmp_path / "tasks-unrelated").mkdir()
        page = runtime.list_tasks(summary=True, sources={remote}, limit=1)
        assert page.total == 2 and page.has_more
        assert page.status_counts["running"] == page.status_counts["queued"] == 1
        assert [source["id"] for source in page.task_sources] == ["shared", "local", remote]
        assert runtime.list_tasks(sources=set()).total == 0
        assert runtime.list_tasks(sources={"shared"}).items[0]["dir"] == Path(shared["dir"]).as_posix()
        task = runtime.list_tasks(sources={remote}, status="running").items[0]
        assert task["task_source"] == remote and task["runner_location"] == "remote"
        assert task["name"] != task["display_name"] == "trial"
        assert task["task_source_host"] == "render-farm-node"
        assert runtime.get_task_logs(task["name"], log_file_name="run1.log")["content"].strip() == "device-specific-output"
        exact_name = runtime.search_tasks(query="^trial$", search_field="name", use_regex=True, sources={remote},
                                          cancelled=threading.Event(), refresh=False)
        assert [item["name"] for item in exact_name.items] == [task["name"]]
        def search(sources):
            return runtime.search_tasks(query="device-specific-output", search_field="log", sources=sources,
                                        cancelled=threading.Event(), refresh=False)
        assert search({remote}).items[0]["name"] == task["name"]
        assert search({"shared"}).total == 0
        manager = runtime.task_manager
        assert manager.get_task(task["name"], summary=True, compact=True)["task_source_host"] == task["task_source_host"]
        assert manager._pick_queued_task()[0] is None
        assert manager.active_task_count() == 0
        assert not manager.start_task_now(task["name"])
        assert not manager.request_task_cancel(task["name"])
        assert not manager.delete_tasks([task["name"]])
        assert not (replica.parent / ".trash").exists()
        assert manager.local_activity_counts() == {"running": 0, "queued": 0}
        with monkeypatch.context() as checks:
            checks.setattr("pyruns.core.task_manager.discover_device_roots", lambda *_: pytest.fail("GPU scan read remote roots"))
            manager._sync_gpu_reservations_from_running_tasks()
        assert Path(manager.task_directory(task["name"])) == replica
        with monkeypatch.context() as checks:
            def unexpected_read(*args, **kwargs):
                pytest.fail("state poll reopened an unchanged task payload")
            checks.setattr("pyruns.core.task_manager.read_task_payload_snapshot", unexpected_read)
            manager.refresh_from_disk()
        write_run_result(str(replica), dict(run_index=1, runner_id="remote-runner", attempt_id="", status="completed",
                                            finish_time="2026-11-21_12-00-00", exit_code=0))
        manager.refresh_from_disk()
        assert manager.get_task(task["name"])["status"] == "completed"
        assert (replica / "task_info.json").read_bytes() == before
        second = "tasks-render-farm-node-bbbbbbbbbbbb"
        (tmp_path / second).mkdir()
        assert manager.sync_task_dirs_from_disk()
        assert manager.task_sources()[-1] == {"id": second, "host": "render-farm-node"}
    finally:
        runtime.shutdown()


def test_web_manages_synced_tasks_in_their_source_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(TaskManager, "_scheduler_loop", lambda self: None)
    runtime = PyrunsRuntime(str(tmp_path))
    first_source = "tasks-render-node-aaaaaaaaaaaa"
    second_source = "tasks-render-node-bbbbbbbbbbbb"
    try:
        shared = runtime.task_generator.create_task("renamed", {"source": "shared"})
        first = TaskGenerator(str(tmp_path / first_source)).create_task("trial", {"source": "first"})
        second = TaskGenerator(str(tmp_path / second_source)).create_task("renamed", {"source": "second"})
        original = runtime.list_tasks(sources={first_source}).items[0]
        original_id = original["name"]

        assert runtime.set_task_pin(original_id, True)["pinned"] is True
        reordered = runtime.reorder_tasks([{"name": original_id}, {"name": "renamed"}])
        assert [task["name"] for task in reordered["items"]] == [original_id, "renamed"]
        runtime.update_task_notes(original_id, "synced notes", "", expected_task_identity=original["task_identity"])
        runtime.update_task_env(original_id, {"MODE": "review"}, {}, expected_task_identity=original["task_identity"])
        edited = load_task_metadata(first["dir"])
        assert (edited["pinned"], edited["task_order"], edited["notes"], edited["env"]) == (
            True, 0, "synced notes", {"MODE": "review"},
        )
        assert load_task_metadata(shared["dir"]).get("notes", "") == ""

        renamed = runtime.rename_task(original_id, "renamed")
        assert renamed["display_name"] == "renamed" and renamed["name"] != "renamed"
        assert renamed["task_source"] == first_source and renamed["task_identity"] == original["task_identity"]
        assert not Path(first["dir"]).exists()
        assert runtime.list_tasks(sources={first_source}, force_refresh=True).items[0]["name"] == renamed["name"]
        assert runtime.get_task(original_id) is None
        peer = runtime.list_tasks(sources={second_source}).items[0]
        assert runtime.delete_tasks_batch([renamed["name"], peer["name"]])["count"] == 2
        assert not Path(renamed["dir"]).exists() and not Path(second["dir"]).exists()
        assert Path(shared["dir"]).exists()

        manager = runtime.task_manager
        trash_names = [record["trash_name"] for record in _trash_records(manager)]
        assert trash_names == [f"renamed@{first_source}", f"renamed@{second_source}"]
        cmd_restore(SimpleNamespace(json_output=True), SimpleNamespace(tasks=trash_names), manager)
        assert load_task_metadata(renamed["dir"])["notes"] == "synced notes"
        assert Path(second["dir"]).exists()
        assert Path(shared["dir"]).exists()
    finally:
        runtime.shutdown()
