"""Failure-atomicity regression tests for task and launcher metadata creation."""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import pyruns
import pyruns.core.task_generator as task_generator_module
import pyruns.launcher as launcher
import pyruns.utils.info_io as info_io
from pyruns.cli import commands
from pyruns._config import (
    ACTIVE_WORKSPACE_FILENAME,
    CONFIG_DEFAULT_FILENAME,
    CONFIG_FILENAME,
    DEFAULT_ROOT_NAME,
    SCRIPT_INFO_FILENAME,
)
from pyruns.core.task_generator import TaskGenerator
from pyruns.core.task_manager import TaskManager


def _create_task_in_process(tasks_root, value, barrier, result_queue):
    """Create one task after exposing its first reserved candidate to the test."""

    real_write_payload = task_generator_module.write_task_payload
    first_payload = True
    initial_name = ""

    def synchronize_first_payload(task_dir, **kwargs):
        nonlocal first_payload, initial_name
        if first_payload:
            first_payload = False
            info = json.loads(
                (Path(task_dir) / "task_info.json").read_text(encoding="utf-8")
            )
            initial_name = info["name"]
            barrier.wait(timeout=10)
        return real_write_payload(task_dir, **kwargs)

    task_generator_module.write_task_payload = synchronize_first_payload
    try:
        task = TaskGenerator(root_dir=tasks_root).create_task("process", {"value": value})
        result_queue.put(("ok", task["name"], initial_name, value))
    except BaseException as exc:
        result_queue.put(("error", type(exc).__name__, str(exc), value))


def test_create_task_is_complete_before_atomic_publish(tmp_path, monkeypatch):
    tasks_root = tmp_path / "tasks"
    generator = TaskGenerator(root_dir=str(tasks_root))
    real_rename = os.rename
    observed: dict[str, Path] = {}

    def inspect_publish(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path.name.startswith(".pyruns-task-"):
            assert source_path.parent == tasks_root
            assert destination_path == tasks_root / "alpha"
            assert not destination_path.exists()
            assert (source_path / "task_info.json").is_file()
            assert (source_path / CONFIG_FILENAME).is_file()
            assert (source_path / "run_logs").is_dir()
            reservation = Path(generator._task_name_lock_path("alpha"))
            assert reservation.is_file()
            owner = json.loads(reservation.read_text(encoding="ascii"))
            assert owner["host"] == task_generator_module._TASK_NAME_LOCK_OWNER_HOST
            assert owner["pid"] == os.getpid()
            assert owner["process_create_time"] is not None
            assert len(owner["token"]) == 32
            observed["source"] = source_path
            observed["destination"] = destination_path
        return real_rename(source, destination)

    monkeypatch.setattr(task_generator_module.os, "rename", inspect_publish)

    task = generator.create_task("alpha", {"value": 1})

    assert observed["destination"] == Path(task["dir"])
    assert not observed["source"].exists()
    saved_info = json.loads(
        (Path(task["dir"]) / "task_info.json").read_text(encoding="utf-8")
    )
    assert saved_info["name"] == "alpha"
    assert not Path(generator._task_name_lock_path("alpha")).exists()


def test_concurrent_creators_reserve_distinct_names_before_writing(tmp_path, monkeypatch):
    tasks_root = tmp_path / "tasks"
    generators = [
        TaskGenerator(root_dir=str(tasks_root)),
        TaskGenerator(root_dir=str(tasks_root)),
    ]
    barrier = threading.Barrier(2)
    observed_names: list[str] = []
    observed_threads: set[int] = set()
    observation_lock = threading.Lock()
    real_write_payload = task_generator_module.write_task_payload

    def synchronize_first_payload(task_dir, **kwargs):
        thread_id = threading.get_ident()
        should_wait = False
        with observation_lock:
            if thread_id not in observed_threads:
                observed_threads.add(thread_id)
                info = json.loads(
                    (Path(task_dir) / "task_info.json").read_text(encoding="utf-8")
                )
                observed_names.append(info["name"])
                should_wait = True
        if should_wait:
            barrier.wait(timeout=5)
        return real_write_payload(task_dir, **kwargs)

    monkeypatch.setattr(
        task_generator_module,
        "write_task_payload",
        synchronize_first_payload,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(generators[index].create_task, "same", {"value": index})
            for index in range(2)
        ]
        tasks = [future.result(timeout=10) for future in futures]

    assert len(set(observed_names)) == 2
    assert len({task["name"] for task in tasks}) == 2
    assert "same" in {task["name"] for task in tasks}
    persisted_values = {
        yaml.safe_load(
            (Path(task["dir"]) / CONFIG_FILENAME).read_text(encoding="utf-8")
        )["value"]
        for task in tasks
    }
    assert persisted_values == {0, 1}
    assert list(tasks_root.glob(".pyruns-create-*.lock")) == []


def test_foreign_name_reservation_is_never_removed_or_reused(tmp_path):
    tasks_root = tmp_path / "tasks"
    generator = TaskGenerator(root_dir=str(tasks_root))
    reservation = Path(generator._task_name_lock_path("alpha"))
    reservation.write_text("pid=foreign\n", encoding="ascii")

    task = generator.create_task("alpha", {"value": 1})

    assert task["name"].startswith("alpha_")
    assert task["name"] != "alpha"
    assert reservation.read_text(encoding="ascii") == "pid=foreign\n"
    assert not (tasks_root / "alpha").exists()


def test_reserved_empty_target_is_not_replaced_by_another_creator(tmp_path):
    tasks_root = tmp_path / "tasks"
    owner = TaskGenerator(root_dir=str(tasks_root))
    competitor = TaskGenerator(root_dir=str(tasks_root))
    reservation = owner._try_reserve_task_name("alpha")
    assert reservation is not None
    empty_target = tasks_root / "alpha"
    empty_target.mkdir()

    try:
        task = competitor.create_task("alpha", {"value": 1})
    finally:
        owner._release_task_name_reservation(reservation)

    assert empty_target.is_dir()
    assert list(empty_target.iterdir()) == []
    assert task["name"].startswith("alpha_")
    assert Path(task["dir"]).is_dir()


def test_processes_reserve_distinct_names_before_publishing(tmp_path):
    if os.name == "nt":
        pytest.skip("fork-only process test avoids creating Windows console processes")
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("fork multiprocessing is unavailable")

    tasks_root = tmp_path / "tasks"
    TaskGenerator(root_dir=str(tasks_root))
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_create_task_in_process,
            args=(str(tasks_root), value, barrier, result_queue),
        )
        for value in (1, 2)
    ]

    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        assert [process.exitcode for process in processes] == [0, 0]
        results = [result_queue.get(timeout=2) for _ in processes]
    except queue.Empty as exc:
        pytest.fail(f"concurrent creator did not report a result: {exc}")
    finally:
        result_queue.close()
        result_queue.join_thread()

    assert {result[0] for result in results} == {"ok"}
    assert len({result[1] for result in results}) == 2
    assert len({result[2] for result in results}) == 2
    assert list(tasks_root.glob(".pyruns-create-*.lock")) == []


def test_create_task_failure_removes_private_staging_directory(tmp_path, monkeypatch):
    tasks_root = tmp_path / "tasks"
    generator = TaskGenerator(root_dir=str(tasks_root))

    def fail_payload(task_dir, **_kwargs):
        (Path(task_dir) / "partial-payload").write_text("partial", encoding="utf-8")
        raise RuntimeError("payload failed")

    monkeypatch.setattr(task_generator_module, "write_task_payload", fail_payload)

    with pytest.raises(RuntimeError, match="payload failed"):
        generator.create_task("broken", {"value": 1})

    assert list(tasks_root.iterdir()) == []


def test_task_scan_ignores_transactional_staging_directories(tmp_path):
    tasks_root = tmp_path / "tasks"
    tasks_root.mkdir()
    (tasks_root / ".pyruns-task-incomplete").mkdir()

    manager = TaskManager(tasks_dir=str(tasks_root), lazy_scan=False)
    try:
        assert manager.list_tasks() == []
    finally:
        manager.shutdown()


def test_task_scan_rejects_link_alias_to_another_task(tmp_path):
    tasks_root = tmp_path / "tasks"
    generator = TaskGenerator(root_dir=str(tasks_root))
    victim = generator.create_task("victim", {"value": 1})
    alias = tasks_root / "alias"
    try:
        alias.symlink_to(Path(victim["dir"]), target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable")

    manager = TaskManager(tasks_dir=str(tasks_root), lazy_scan=False)
    try:
        assert [task["name"] for task in manager.list_tasks()] == ["victim"]
        with pytest.raises(ValueError, match="must not be a symlink"):
            info_io.update_task_info(str(alias), lambda info: info.update({"pinned": True}))
        assert info_io.load_task_info(victim["dir"], raise_error=True)["pinned"] is False
    finally:
        manager.shutdown()


def test_bootstrap_rejects_linked_config_default_without_touching_target(tmp_path):
    script = tmp_path / "train.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    custom = tmp_path / "custom.yaml"
    custom.write_text("value: replacement\n", encoding="utf-8")
    workspace = Path(launcher.workspace_root_for_script(str(script)))
    workspace.mkdir(parents=True)
    victim = tmp_path / "victim.yaml"
    victim.write_text("value: keep\n", encoding="utf-8")
    config_default = workspace / "config_default.yaml"
    try:
        config_default.symlink_to(victim)
    except (NotImplementedError, OSError):
        pytest.skip("file symlinks are unavailable")

    with pytest.raises(ValueError, match="must not be a symlink"):
        launcher.bootstrap_workspace(str(script), custom_yaml=str(custom))

    assert victim.read_text(encoding="utf-8") == "value: keep\n"
    assert config_default.is_symlink()


def test_ensure_config_default_rejects_simulated_reparse_file(tmp_path, monkeypatch):
    config_default = tmp_path / "config_default.yaml"
    config_default.write_text("value: keep\n", encoding="utf-8")
    real_check = info_io._path_is_link_or_reparse

    def fake_reparse(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(config_default)):
            return True
        return real_check(path)

    monkeypatch.setattr(info_io, "_path_is_link_or_reparse", fake_reparse)

    with pytest.raises(ValueError, match="regular file"):
        pyruns.ensure_config_default(str(tmp_path))

    assert config_default.read_text(encoding="utf-8") == "value: keep\n"


def test_ensure_config_default_rejects_simulated_reparse_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    workspace.mkdir(parents=True)
    real_check = info_io._path_is_link_or_reparse

    def fake_reparse(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(workspace)):
            return True
        return real_check(path)

    monkeypatch.setattr(info_io, "_path_is_link_or_reparse", fake_reparse)

    with pytest.raises(ValueError, match="Managed workspace path must not contain"):
        pyruns.ensure_config_default(str(workspace))
    assert not (workspace / CONFIG_DEFAULT_FILENAME).exists()


def test_delete_rejects_reparse_trash_before_mutating_task(tmp_path, monkeypatch):
    tasks_root = tmp_path / "tasks"
    generator = TaskGenerator(root_dir=str(tasks_root))
    task = generator.create_task("keep", {"value": 1})
    trash = tasks_root / ".trash"
    trash.mkdir()
    real_check = info_io._path_is_link_or_reparse

    def fake_reparse(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(trash.resolve())):
            return True
        return real_check(path)

    monkeypatch.setattr(info_io, "_path_is_link_or_reparse", fake_reparse)
    manager = TaskManager(tasks_dir=str(tasks_root), lazy_scan=False)
    try:
        with pytest.raises(ValueError, match="reparse point"):
            manager.delete_tasks(["keep"])
        assert Path(task["dir"]).is_dir()
        assert info_io.load_task_info(task["dir"], raise_error=True)["status"] == "pending"
    finally:
        manager.shutdown()


def test_delete_uses_exact_unique_trash_destination_without_nesting(tmp_path, monkeypatch):
    tasks_root = tmp_path / "tasks"
    generator = TaskGenerator(root_dir=str(tasks_root))
    task = generator.create_task("alpha", {"value": 1})
    trash = tasks_root / ".trash"
    trash.mkdir()
    (trash / "alpha").mkdir()
    first_conflict = trash / "alpha_2026-08-09_12-00-00_aaaaaaaa"
    first_conflict.mkdir()
    sentinel = first_conflict / "keep.txt"
    sentinel.write_text("do not nest here", encoding="utf-8")
    suffixes = iter(("aaaaaaaa", "bbbbbbbb"))

    monkeypatch.setattr(
        "pyruns.core.task_manager.get_now_str",
        lambda: "2026-08-09_12-00-00",
    )
    monkeypatch.setattr(
        "pyruns.core.task_manager.uuid.uuid4",
        lambda: SimpleNamespace(hex=next(suffixes)),
    )

    manager = TaskManager(tasks_dir=str(tasks_root), lazy_scan=False)
    try:
        assert manager.delete_tasks(["alpha"]) == ["alpha"]
    finally:
        manager.shutdown()

    destination = trash / "alpha_2026-08-09_12-00-00_bbbbbbbb"
    assert destination.is_dir()
    assert (destination / "task_info.json").is_file()
    assert sentinel.read_text(encoding="utf-8") == "do not nest here"
    assert not (first_conflict / "alpha").exists()
    assert not Path(task["dir"]).exists()


def test_restore_rejects_invalid_metadata_before_moving(tmp_path):
    tasks_root = tmp_path / "tasks"
    generator = TaskGenerator(root_dir=str(tasks_root))
    generator.create_task("broken", {"value": 1})
    manager = TaskManager(tasks_dir=str(tasks_root), lazy_scan=False)
    try:
        assert manager.delete_tasks(["broken"]) == ["broken"]
        trash_entry = tasks_root / ".trash" / "broken"
        (trash_entry / "task_info.json").unlink()

        with pytest.raises(commands.CliError, match="invalid task metadata"):
            commands.cmd_restore(
                SimpleNamespace(json_output=False),
                SimpleNamespace(tasks=["broken"]),
                manager,
            )

        assert trash_entry.is_dir()
        assert not (tasks_root / "broken").exists()
    finally:
        manager.shutdown()


def test_batch_restore_rolls_back_earlier_moves_when_later_rename_fails(tmp_path, monkeypatch):
    tasks_root = tmp_path / "tasks"
    generator = TaskGenerator(root_dir=str(tasks_root))
    generator.create_task("first", {"value": 1})
    generator.create_task("second", {"value": 2})
    manager = TaskManager(tasks_dir=str(tasks_root), lazy_scan=False)
    try:
        assert manager.delete_tasks(["first", "second"]) == ["first", "second"]
        real_rename = os.rename

        def fail_second_restore(source, destination):
            source_path = Path(source)
            if source_path.parent.name == ".trash" and source_path.name == "second":
                raise PermissionError("second restore blocked")
            return real_rename(source, destination)

        monkeypatch.setattr(commands.os, "rename", fail_second_restore)

        with pytest.raises(commands.CliError, match="second restore blocked"):
            commands.cmd_restore(
                SimpleNamespace(json_output=False),
                SimpleNamespace(tasks=["first", "second"]),
                manager,
            )

        assert (tasks_root / ".trash" / "first").is_dir()
        assert (tasks_root / ".trash" / "second").is_dir()
        assert not (tasks_root / "first").exists()
        assert not (tasks_root / "second").exists()
        assert list(tasks_root.glob(".pyruns-create-*.lock")) == []
    finally:
        manager.shutdown()


def test_restore_reservation_forces_concurrent_creator_to_use_suffix(tmp_path, monkeypatch):
    tasks_root = tmp_path / "tasks"
    generator = TaskGenerator(root_dir=str(tasks_root))
    generator.create_task("alpha", {"value": "restored"})
    manager = TaskManager(tasks_dir=str(tasks_root), lazy_scan=False)
    created_during_restore: list[dict] = []
    try:
        assert manager.delete_tasks(["alpha"]) == ["alpha"]
        real_rename = os.rename
        injected = False

        def create_at_publish_boundary(source, destination):
            nonlocal injected
            source_path = Path(source)
            if source_path.parent.name == ".trash" and source_path.name == "alpha" and not injected:
                injected = True
                created_during_restore.append(generator.create_task("alpha", {"value": "new"}))
            return real_rename(source, destination)

        monkeypatch.setattr(commands.os, "rename", create_at_publish_boundary)

        assert commands.cmd_restore(
            SimpleNamespace(json_output=False),
            SimpleNamespace(tasks=["alpha"]),
            manager,
        ) == 0

        assert (tasks_root / "alpha").is_dir()
        assert len(created_during_restore) == 1
        assert created_during_restore[0]["name"].startswith("alpha_")
        assert created_during_restore[0]["name"] != "alpha"
        assert Path(created_during_restore[0]["dir"]).is_dir()
        assert list(tasks_root.glob(".pyruns-create-*.lock")) == []
    finally:
        manager.shutdown()


def test_explicit_shell_task_name_fails_while_exact_name_is_reserved(tmp_path):
    tasks_root = tmp_path / "tasks"
    holder = TaskGenerator(root_dir=str(tasks_root))
    creator = TaskGenerator(root_dir=str(tasks_root))
    reservation = holder.reserve_exact_task_name("exact")
    assert reservation is not None
    try:
        with pytest.raises(ValueError, match="already exists or is being created"):
            creator.create_shell_task("exact", "echo exact\n", exact_name=True)

        automatic = creator.create_shell_task("exact", "echo automatic\n")
        assert automatic["name"] != "exact"
        assert automatic["name"].startswith("exact_")
    finally:
        holder.release_task_name_reservation(reservation)


def test_task_name_reservation_recovers_only_an_aged_invalid_lock(tmp_path):
    tasks_root = tmp_path / "tasks"
    generator = TaskGenerator(root_dir=str(tasks_root))
    lock_path = Path(generator._task_name_lock_path("stale-name"))
    lock_path.write_bytes(b"")

    snapshot = generator._task_name_lock_snapshot(str(lock_path))
    assert snapshot is not None
    assert generator._task_name_lock_is_stale(snapshot, min_age_sec=30) is False
    with pytest.raises(ValueError, match="already exists or is being created"):
        generator.create_task("stale-name", {"value": 1}, exact_name=True)
    assert lock_path.exists()

    os.utime(lock_path, (1, 1))
    created = generator.create_task("stale-name", {"value": 2}, exact_name=True)

    assert created["name"] == "stale-name"
    assert not lock_path.exists()


def test_task_name_reservation_keeps_valid_foreign_owner_even_when_old(tmp_path, monkeypatch):
    tasks_root = tmp_path / "tasks"
    generator = TaskGenerator(root_dir=str(tasks_root))
    lock_path = Path(generator._task_name_lock_path("foreign"))
    lock_path.write_text(
        json.dumps({
            "host": f"{task_generator_module._TASK_NAME_LOCK_OWNER_HOST}-foreign",
            "pid": 4242,
            "process_create_time": 1.0,
            "token": "foreign-token",
        }),
        encoding="utf-8",
    )
    os.utime(lock_path, (1, 1))
    monkeypatch.setattr(task_generator_module.time, "time", lambda: 1_000_000.0)

    snapshot = generator._task_name_lock_snapshot(str(lock_path))
    assert snapshot is not None
    assert generator._task_name_lock_is_stale(snapshot, min_age_sec=0) is False
    assert generator._remove_stale_task_name_lock(str(lock_path)) is False
    assert lock_path.exists()


def test_create_task_publish_failure_removes_private_staging_directory(tmp_path, monkeypatch):
    tasks_root = tmp_path / "tasks"
    generator = TaskGenerator(root_dir=str(tasks_root))

    def fail_publish(_source, _destination):
        raise PermissionError("publish blocked")

    monkeypatch.setattr(task_generator_module.os, "rename", fail_publish)

    with pytest.raises(PermissionError, match="publish blocked"):
        generator.create_task("blocked", {"value": 1})

    assert list(tasks_root.iterdir()) == []


def test_batch_failure_rolls_back_only_tasks_created_by_batch(tmp_path, monkeypatch):
    tasks_root = tmp_path / "tasks"
    generator = TaskGenerator(root_dir=str(tasks_root))
    existing = tasks_root / "batch_1-of-3"
    existing.mkdir()
    sentinel = existing / "keep.txt"
    sentinel.write_text("existing task", encoding="utf-8")

    real_write_payload = task_generator_module.write_task_payload
    calls = 0

    def fail_second_payload(task_dir, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second task failed")
        return real_write_payload(task_dir, **kwargs)

    monkeypatch.setattr(task_generator_module, "write_task_payload", fail_second_payload)

    with pytest.raises(RuntimeError, match="second task failed"):
        generator.create_tasks([{"value": 1}, {"value": 2}, {"value": 3}], "batch")

    assert [path.name for path in tasks_root.iterdir()] == [existing.name]
    assert sentinel.read_text(encoding="utf-8") == "existing task"


def test_batch_failure_preserves_an_earlier_task_started_by_another_runner(
    tmp_path,
    monkeypatch,
):
    tasks_root = tmp_path / "tasks"
    generator = TaskGenerator(root_dir=str(tasks_root))
    real_write_payload = task_generator_module.write_task_payload
    calls = 0

    def start_first_then_fail_second(task_dir, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            published = [
                path
                for path in tasks_root.iterdir()
                if path.is_dir() and not path.name.startswith(".")
            ]
            assert len(published) == 1

            def mark_started(info):
                info.update(
                    {
                        "status": "running",
                        "run_index": 1,
                        "runner_id": "other-host:4242:token",
                        "runner_host": "other-host",
                        "lease_until": 9_999_999_999.0,
                    }
                )

            info_io.update_task_info(str(published[0]), mark_started)
            raise RuntimeError("second task failed after first started")
        return real_write_payload(task_dir, **kwargs)

    monkeypatch.setattr(
        task_generator_module,
        "write_task_payload",
        start_first_then_fail_second,
    )

    with pytest.raises(RuntimeError, match="second task failed after first started"):
        generator.create_tasks([{"value": 1}, {"value": 2}], "batch")

    published = [
        path
        for path in tasks_root.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ]
    assert len(published) == 1
    preserved = info_io.load_task_info(str(published[0]), raise_error=True)
    assert preserved["status"] == "running"
    assert preserved["runner_id"] == "other-host:4242:token"
    assert "_creation_rollback" not in preserved
    assert list(tasks_root.glob(".pyruns-create-*.lock")) == []


def test_task_generator_rejects_symlinked_tasks_root_before_writing(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_root = tmp_path / "linked-tasks"
    try:
        linked_root.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(ValueError, match="must not be a symlink"):
        TaskGenerator(root_dir=str(linked_root))

    assert list(outside.iterdir()) == []


def test_script_info_replace_failure_preserves_previous_file(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script_info = workspace / SCRIPT_INFO_FILENAME
    original = '{"script_name": "old"}\n'
    script_info.write_text(original, encoding="utf-8")

    def fail_replace(_source, _destination):
        raise PermissionError("replace blocked")

    monkeypatch.setattr(info_io, "_replace_with_retry", fail_replace)

    with pytest.raises(PermissionError, match="replace blocked"):
        launcher._write_script_info(str(workspace), {"script_name": "new"})

    assert script_info.read_text(encoding="utf-8") == original
    assert list(workspace.glob(f".{SCRIPT_INFO_FILENAME}.*.tmp")) == []


def test_active_marker_replace_failure_preserves_previous_file(tmp_path, monkeypatch):
    project_root = tmp_path / "_pyruns_"
    workspace = project_root / "train"
    workspace.mkdir(parents=True)
    marker = project_root / ACTIVE_WORKSPACE_FILENAME
    marker.write_text("old", encoding="utf-8")
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def record_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    def fail_replace(source, destination):
        assert Path(source).parent == project_root
        assert Path(destination) == marker
        assert Path(source).read_text(encoding="utf-8") == "train"
        raise PermissionError("replace blocked")

    monkeypatch.setattr(launcher.os, "fsync", record_fsync)
    monkeypatch.setattr(launcher.os, "replace", fail_replace)

    with pytest.raises(PermissionError, match="replace blocked"):
        launcher.mark_workspace_active(str(workspace))

    assert fsync_calls
    assert marker.read_text(encoding="utf-8") == "old"
    assert list(project_root.glob(f".{ACTIVE_WORKSPACE_FILENAME}.*.tmp")) == []


@pytest.mark.parametrize("metadata_kind", ["script_info", "active_marker"])
def test_workspace_metadata_fsyncs_parent_after_atomic_replace(
    tmp_path,
    monkeypatch,
    metadata_kind,
):
    workspace = tmp_path / ("_pyruns_/train" if metadata_kind == "active_marker" else "workspace")
    workspace.mkdir(parents=True)
    synced: list[str] = []

    monkeypatch.setattr(
        launcher,
        "_fsync_parent_directory",
        lambda path: synced.append(path),
    )

    if metadata_kind == "script_info":
        launcher._write_script_info(str(workspace), {"script_name": "train"})
        target = workspace / SCRIPT_INFO_FILENAME
        assert json.loads(target.read_text(encoding="utf-8"))["script_name"] == "train"
    else:
        launcher.mark_workspace_active(str(workspace))
        target = workspace.parent / ACTIVE_WORKSPACE_FILENAME
        assert target.read_text(encoding="utf-8") == "train"

    assert len(synced) == 1
    assert Path(synced[0]) == target
