"""Storage behavior across migration, concurrency, and interrupted commits."""

import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

import pyruns
from pyruns._config import ENV_KEY_CONFIG, ENV_KEY_RUN_INDEX
from pyruns.utils import info_io, lock_queue, track_store
from pyruns.utils.info_io import (
    append_task_track, load_task_info, load_task_metadata, save_task_info,
    task_info_lock, update_task_info, update_task_metadata,
)


@pytest.fixture
def task(tmp_path, monkeypatch):
    directory = tmp_path / "tasks" / "metrics"
    directory.mkdir(parents=True)
    (directory / "config.yaml").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(track_store, "INLINE_TRACK_POINTS", 4)
    monkeypatch.setenv(ENV_KEY_CONFIG, str(directory / "config.yaml"))
    monkeypatch.setenv(ENV_KEY_RUN_INDEX, "1")
    save_task_info(str(directory), {"name": "metrics", "run_index": 1, "status": "running"})
    return directory


def externalize(task):
    for index in range(4):
        append_task_track(str(task), {"loss": index}, run_index=1)
    assert track_store.TRACK_STORE_KEY in load_task_metadata(str(task), raise_error=True)


def test_inline_migration_and_append_preserve_exact_history(task):
    for value in (0, 1, 2):
        pyruns.track(loss=value)
    raw = json.loads((task / "task_info.json").read_text())
    assert raw["tracks"] == [{"loss": [0, 1, 2]}]
    assert "track_store" not in raw

    pyruns.track(loss=3)
    raw = json.loads((task / "task_info.json").read_text())
    assert raw["tracks"] == [{}]
    before = (task / "task_info.json").read_bytes()
    for value in (4, 5, 6):
        pyruns.track(loss=value)
    assert (task / "task_info.json").read_bytes() == before
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": list(range(7))}]


def test_record_and_lifecycle_updates_do_not_read_external_history(task, monkeypatch):
    externalize(task)
    with monkeypatch.context() as patch:
        patch.setattr(track_store, "read_tracks", lambda *a, **kw: pytest.fail("metadata path loaded curves"))
        pyruns.record(accuracy=0.9)
        update_task_metadata(str(task), lambda info: info.update(status="completed", exit_codes=[0]))
    info = load_task_info(str(task), raise_error=True)
    assert info["records"] == [{"accuracy": 0.9}]
    assert info["tracks"] == [{"loss": [0, 1, 2, 3]}]
    assert info["status"] == "completed"


def test_public_update_and_save_replace_full_history_and_return_detached_values(task):
    externalize(task)
    append_task_track(str(task), {"loss": 4})
    replacement = {"score": [{"value": 0.7}]}

    def update(info):
        assert info["tracks"] == [{"loss": [0, 1, 2, 3, 4]}]
        info["tracks"][0] = replacement
        info["notes"] = "replaced"

    returned = update_task_info(str(task), update)
    replacement["score"][0]["value"] = 100
    returned["tracks"][0]["score"][0]["value"] = 200
    info = load_task_info(str(task), raise_error=True)
    assert info["tracks"] == [{"score": [{"value": 0.7}]}]
    info["tracks"] = [{"loss": [9]}, {"accuracy": [0.5]}]
    save_task_info(str(task), info)
    info["tracks"][0]["loss"].append(10)
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": [9]}, {"accuracy": [0.5]}]


def test_failed_pointer_switch_keeps_old_generation_and_retry_prunes_orphans(task, monkeypatch):
    externalize(task)
    original = load_task_info(str(task), raise_error=True)
    with monkeypatch.context() as patch:
        patch.setattr(info_io, "_replace_with_retry", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError, match="disk full"):
            update_task_info(str(task), lambda info: info["tracks"][0]["loss"].append(100))
    assert load_task_info(str(task), raise_error=True) == original
    append_task_track(str(task), {"loss": 4})
    updated = update_task_info(str(task), lambda info: info.update(notes="retry"))
    assert updated["tracks"] == [{"loss": [0, 1, 2, 3, 4]}]
    with sqlite3.connect(task / track_store.TRACK_STORE_FILENAME) as connection:
        assert connection.execute("SELECT count(*) FROM generations").fetchone()[0] == 1


def test_interruption_after_pointer_switch_exposes_complete_new_generation(task, monkeypatch):
    externalize(task)
    original_write = info_io._write_task_info_unlocked

    class Interrupted(BaseException):
        pass

    def write_then_interrupt(*args):
        original_write(*args)
        raise Interrupted()

    with monkeypatch.context() as patch:
        patch.setattr(info_io, "_write_task_info_unlocked", write_then_interrupt)
        with pytest.raises(Interrupted):
            update_task_info(str(task), lambda info: info["tracks"][0]["loss"].append(4))
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": [0, 1, 2, 3, 4]}]


def test_full_reader_rechecks_pointer_after_concurrent_replacement(task, monkeypatch):
    externalize(task)
    original_load = info_io.load_task_metadata
    first_read = True

    def load_then_replace(*args, **kwargs):
        nonlocal first_read
        info = original_load(*args, **kwargs)
        if first_read:
            first_read = False
            update_task_info(str(task), lambda data: data["tracks"][0]["loss"].append(4))
        return info

    monkeypatch.setattr(info_io, "load_task_metadata", load_then_replace)
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": [0, 1, 2, 3, 4]}]


def test_full_read_inside_update_keeps_other_writers_out(task):
    externalize(task)

    def attempt_lock():
        with pytest.raises(TimeoutError):
            with task_info_lock(str(task), timeout_sec=0.01):
                pytest.fail("nested read released the outer writer lock")

    def update(info):
        assert load_task_info(str(task), raise_error=True)["tracks"] == info["tracks"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(attempt_lock).result(timeout=2)
        info["notes"] = "updated"

    update_task_info(str(task), update)
    assert load_task_info(str(task), raise_error=True)["notes"] == "updated"


@pytest.mark.parametrize("damage", ["missing", "corrupt", "missing-generation"])
def test_damaged_store_is_reported_without_breaking_control_metadata(task, damage):
    externalize(task)
    database = task / track_store.TRACK_STORE_FILENAME
    if damage == "missing":
        database.unlink()
    elif damage == "corrupt":
        database.write_bytes(b"not a database")
    else:
        with sqlite3.connect(database) as connection:
            connection.execute("DELETE FROM generations")
    with pytest.raises((FileNotFoundError, sqlite3.DatabaseError, ValueError)):
        load_task_info(str(task), raise_error=True)
    assert load_task_metadata(str(task), raise_error=True)["status"] == "running"
    update_task_metadata(str(task), lambda info: info.update(status="failed"))
    assert load_task_metadata(str(task))["status"] == "failed"


def test_large_curves_do_not_exhaust_lifecycle_metadata_budget(task, monkeypatch):
    monkeypatch.setattr(info_io, "MAX_TASK_INFO_BYTES", 2048)
    values = list(range(2000))
    info = load_task_info(str(task), raise_error=True)
    info["tracks"] = [{"loss": values}]
    save_task_info(str(task), info)
    append_task_track(str(task), {"loss": 2000})
    update_task_metadata(str(task), lambda info: info.update(status="completed", exit_codes=[0], finish_times=["done"]))
    assert (task / "task_info.json").stat().st_size < 2048
    assert load_task_info(str(task), raise_error=True)["tracks"][0]["loss"] == list(range(2001))


def test_concurrent_track_and_record_updates_preserve_all_values_and_run_isolation(task):
    externalize(task)

    def write(worker):
        for value in range(15):
            append_task_track(str(task), {"value": worker * 100 + value}, run_index=worker + 1)
            update_task_metadata(str(task), lambda info: info["records"][0].update({f"worker{worker}": value}))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(4)))
    info = load_task_info(str(task), raise_error=True)
    for worker in range(4):
        assert info["tracks"][worker]["value"] == [worker * 100 + value for value in range(15)]
        assert info["records"][0][f"worker{worker}"] == 14
    assert info["tracks"][0]["loss"] == [0, 1, 2, 3]


def test_retry_after_committed_append_does_not_duplicate_point(task, monkeypatch):
    externalize(task)
    real_connect = track_store._connect
    fail_once = True

    class CommitThenFail:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, *args):
            return self.connection.execute(*args)

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            nonlocal fail_once
            result = self.connection.__exit__(*args)
            if args[0] is None and fail_once:
                fail_once = False
                raise sqlite3.OperationalError("commit result interrupted")
            return result

    @contextmanager
    def flaky_connect(*args, **kwargs):
        with real_connect(*args, **kwargs) as connection:
            yield CommitThenFail(connection)

    with monkeypatch.context() as patch:
        patch.setattr(track_store, "_connect", flaky_connect)
        append_task_track(str(task), {"loss": 4})
    assert fail_once is False
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": [0, 1, 2, 3, 4]}]


def test_failed_generation_cleanup_does_not_turn_committed_update_into_failure(task, monkeypatch):
    externalize(task)
    monkeypatch.setattr(track_store, "prune_generations", lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("busy")))
    update_task_info(str(task), lambda info: info["tracks"][0]["loss"].append(4))
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": [0, 1, 2, 3, 4]}]


def test_metadata_only_update_rejects_curve_replacement(task):
    externalize(task)
    with pytest.raises(ValueError, match="full task update"):
        update_task_metadata(str(task), lambda info: info.update(tracks=[{"loss": [100]}]))
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": [0, 1, 2, 3]}]


def test_full_read_does_not_require_a_writable_task_lock(task, monkeypatch):
    externalize(task)
    monkeypatch.setattr(info_io, "task_info_lock", lambda *a, **kw: pytest.fail("read attempted a write lock"))
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": [0, 1, 2, 3]}]


@pytest.mark.skipif(os.name == "nt", reason="POSIX read-only permissions")
def test_full_read_from_readonly_task_directory(task):
    externalize(task)
    files = list(task.iterdir())
    try:
        for path in files:
            path.chmod(0o444)
        task.chmod(0o555)
        assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": [0, 1, 2, 3]}]
    finally:
        task.chmod(0o755)
        for path in files:
            path.chmod(0o644)


@pytest.mark.parametrize("invalid", [float("nan"), "oversized"])
def test_invalid_control_metadata_does_not_create_orphan_generations(task, monkeypatch, invalid):
    externalize(task)
    before = (task / "task_info.json").read_bytes()
    monkeypatch.setattr(info_io, "MAX_TASK_INFO_BYTES", 2048)
    notes = "x" * 4096 if invalid == "oversized" else invalid
    with pytest.raises(ValueError):
        update_task_info(str(task), lambda info: info.update(notes=notes))
    assert (task / "task_info.json").read_bytes() == before
    with sqlite3.connect(task / track_store.TRACK_STORE_FILENAME) as connection:
        assert connection.execute("SELECT count(*) FROM generations").fetchone()[0] == 1


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), "x" * 500])
def test_invalid_points_leave_history_unchanged(task, monkeypatch, invalid):
    externalize(task)
    monkeypatch.setattr(track_store, "MAX_TRACK_EVENT_BYTES", 100)
    with pytest.raises(ValueError):
        append_task_track(str(task), {"loss": invalid})
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": [0, 1, 2, 3]}]


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_storage_rejects_linked_database_and_sidecars(task, tmp_path, suffix):
    externalize(task)
    victim = tmp_path / "outside"
    victim.write_bytes(b"protected")
    path = task / (track_store.TRACK_STORE_FILENAME + suffix)
    if path.exists():
        path.unlink()
    try:
        path.symlink_to(victim)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(ValueError, match="symlink|reparse"):
        append_task_track(str(task), {"loss": 4})
    with pytest.raises(ValueError, match="symlink|reparse"):
        load_task_info(str(task), raise_error=True)
    assert victim.read_bytes() == b"protected"


def test_reserved_database_path_does_not_adopt_unrelated_sqlite_file(task):
    with sqlite3.connect(task / track_store.TRACK_STORE_FILENAME) as connection:
        connection.execute("CREATE TABLE unrelated(value)")
    for value in range(3):
        append_task_track(str(task), {"loss": value})
    with pytest.raises(ValueError, match="unrelated database"):
        append_task_track(str(task), {"loss": 3})
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": [0, 1, 2]}]
    with sqlite3.connect(task / track_store.TRACK_STORE_FILENAME) as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == [("unrelated",)]


def test_sqlite_write_failure_warns_once_and_preserves_existing_history(task, monkeypatch, capsys):
    externalize(task)
    monkeypatch.setattr(pyruns, "_metric_warning_keys", set())
    real_connect = track_store._connect

    def fail_writes(*args, **kwargs):
        if not kwargs.get("readonly"):
            raise sqlite3.OperationalError("database or disk is full")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(track_store, "_connect", fail_writes)
    pyruns.track(loss=4)
    pyruns.track(loss=5)
    assert capsys.readouterr().err.count("track() could not save metrics") == 1
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": [0, 1, 2, 3]}]
    update_task_metadata(str(task), lambda info: info.update(status="completed"))


def _child(code, *args):
    return subprocess.Popen(
        [sys.executable, "-c", code, *map(str, args)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.mark.parametrize("commit_delay", [0, 0.08])
def test_multiple_processes_preserve_every_append_and_record(task, commit_delay):
    externalize(task)
    code = """
import sys, time
from pyruns.utils import info_io
from pyruns.utils.info_io import append_task_track, update_task_metadata
directory, worker = sys.argv[1], int(sys.argv[2])
delay = float(sys.argv[3])
original_write = info_io._write_task_info_unlocked
def slow_write(*args, **kwargs):
    if delay:
        time.sleep(delay)
    return original_write(*args, **kwargs)
info_io._write_task_info_unlocked = slow_write
for value in range(20):
    append_task_track(directory, {'worker': worker, 'value': worker * 100 + value})
    update_task_metadata(directory, lambda info: info['records'][0].update({str(worker): value}))
"""
    processes = [_child(code, task, worker, commit_delay) for worker in range(4)]
    try:
        for process in processes:
            output, error = process.communicate(timeout=30)
            if process.returncode != 0:
                owner = info_io._read_lock_owner(str(task / info_io._LOCK_FILENAME))
                waiters = sorted(path.name for path in (task / lock_queue._QUEUE_DIR).glob("*.wait"))
                workers = {worker.pid: worker.poll() for worker in processes}
                pytest.fail(
                    (output + error).decode("utf-8", errors="replace")
                    + f"\nlock_owner={owner}\nwaiters={waiters}\nworkers={workers}"
                )
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()
    info = load_task_info(str(task), raise_error=True)
    assert info["records"] == [{str(worker): 19 for worker in range(4)}]
    assert sorted(info["tracks"][0]["value"]) == [worker * 100 + value for worker in range(4) for value in range(20)]
    assert all(value // 100 == worker for worker, value in zip(info["tracks"][0]["worker"], info["tracks"][0]["value"]))


@pytest.mark.parametrize("phase", ["prepared", "switched", "uncommitted"])
def test_process_exit_at_commit_boundaries_leaves_complete_readable_history(task, phase):
    externalize(task)
    code = """
import os, sqlite3, sys
from pyruns.utils import info_io, track_store
directory, phase = sys.argv[1:]
if phase == 'uncommitted':
    with info_io.task_info_lock(directory):
        descriptor = info_io.load_task_metadata(directory)['track_store']
        with track_store._connect(directory) as connection:
            connection.execute('PRAGMA cache_size=1')
            connection.execute('INSERT INTO points(generation, operation, run_index, payload) VALUES (?, ?, ?, ?)',
                               (descriptor['generation'], 'crash', 1, '{"loss":"' + 'x' * 100000 + '"}'))
            os._exit(23)
module, name = (track_store, 'prepare_generation') if phase == 'prepared' else (info_io, '_write_task_info_unlocked')
original = getattr(module, name)
def exit_after(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(23)
setattr(module, name, exit_after)
info_io.update_task_info(directory, lambda info: info['tracks'][0]['loss'].append(4))
"""
    process = _child(code, task, phase)
    try:
        output, error = process.communicate(timeout=20)
        assert process.returncode == 23, (output, error)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()
    expected = [0, 1, 2, 3, 4] if phase == "switched" else [0, 1, 2, 3]
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": expected}]
    append_task_track(str(task), {"loss": 5})
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": expected + [5]}]
    with sqlite3.connect(task / track_store.TRACK_STORE_FILENAME) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_manager_and_cli_keep_control_lightweight_and_details_complete(task, monkeypatch):
    from pyruns.cli import commands
    from pyruns.core.task_manager import TaskManager

    externalize(task)
    update_task_metadata(str(task), lambda info: info.update(status="completed", run_statuses=["completed"]))
    monkeypatch.setattr(TaskManager, "_scheduler_loop", lambda self: None)
    manager = TaskManager(tasks_dir=str(task.parent), lazy_scan=False)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(track_store, "read_tracks", lambda *a, **kw: pytest.fail("control loaded curves"))
            assert manager.tasks[0]["tracks"] == [{}]
            assert manager.list_tasks(summary=True)[0]["tracks"] == []
            manager.refresh_from_disk(force_all=True)
            assert manager.set_task_pinned("metrics")[0]
            assert manager.update_task_notes("metrics", "note", "")[0]
            assert commands._task_record(manager.tasks[0])["status"] == "completed"
            assert pyruns.get_run_index() == 1
        append_task_track(str(task), {"loss": 4})
        detail = manager.get_task("metrics")
        assert detail["tracks"] == [{"loss": [0, 1, 2, 3, 4]}]
        assert "track_store" not in detail
        assert commands._task_record(manager.tasks[0], detailed=True, selected_run=1)["selected_run"]["track"] == detail["tracks"][0]
        assert manager.rename_task("metrics", "renamed") == (True, "renamed")
        renamed = task.with_name("renamed")
        assert load_task_info(str(renamed), raise_error=True)["tracks"] == detail["tracks"]
        assert manager.delete_tasks(["renamed"]) == ["renamed"]
        from pyruns._config import TRASH_DIR

        trashed = next((task.parent / TRASH_DIR).iterdir())
        assert load_task_info(str(trashed), raise_error=True)["tracks"] == detail["tracks"]
        trashed.rename(renamed)
        manager.scan_disk()
        assert manager.get_task("renamed")["tracks"] == detail["tracks"]
    finally:
        manager.shutdown()


def test_queued_placeholder_trimming_keeps_external_curve_only_run(task):
    from pyruns.core.task_manager import TaskManager

    externalize(task)
    append_task_track(str(task), {"loss": 10}, run_index=2)
    info = update_task_metadata(str(task), lambda data: data.update(status="queued", run_index=3))
    trimmed = TaskManager._strip_queued_placeholder_run(info)
    assert trimmed["run_index"] == 2
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": [0, 1, 2, 3]}, {"loss": [10]}, {}]


def test_failed_append_to_new_slot_can_resume_without_losing_existing_runs(task, monkeypatch):
    externalize(task)
    with monkeypatch.context() as patch:
        patch.setattr(track_store, "append_point", lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("full")))
        with pytest.raises(sqlite3.OperationalError):
            append_task_track(str(task), {"loss": 10}, run_index=2)
    # Reserving a slot precedes the append, so a failed first write can leave
    # an empty run. Resuming that run must neither hide nor duplicate points.
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": [0, 1, 2, 3]}, {}]
    append_task_track(str(task), {"loss": 11}, run_index=2)
    update_task_metadata(str(task), lambda info: info.update(status="completed", exit_codes=[0, 0]))
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": [0, 1, 2, 3]}, {"loss": [11]}]


@pytest.mark.parametrize("blocked_operation", ["read", "remove", "remove-exhausted"])
def test_task_lock_release_retries_transient_windows_sharing_errors(task, monkeypatch, blocked_operation):
    import builtins

    lock_path = str(task / info_io._LOCK_FILENAME)
    original_remove = os.remove
    original_open = builtins.open
    failures = []
    limit = info_io._REPLACE_RETRY_COUNT if blocked_operation == "remove-exhausted" else 1

    def remove(path, *args, **kwargs):
        if str(path) == lock_path and blocked_operation.startswith("remove") and len(failures) < limit:
            failures.append(path)
            raise PermissionError("file is being read by a competing process")
        return original_remove(path, *args, **kwargs)

    def open_file(path, *args, **kwargs):
        if str(path) == lock_path and blocked_operation == "read" and not failures:
            failures.append(path)
            raise PermissionError("sharing violation while checking lock owner")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "remove", remove)
        patch.setattr(builtins, "open", open_file)
        append_task_track(str(task), {"loss": 0})
    assert len(failures) == limit
    assert os.path.exists(lock_path) == (blocked_operation == "remove-exhausted")
    append_task_track(str(task), {"loss": 1})
    assert not os.path.exists(lock_path), "a completed owner must not block the next write"
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": [0, 1]}]


def test_task_lock_creation_retries_temporary_access_denied(task, monkeypatch):
    lock_path = str(task / info_io._LOCK_FILENAME)
    real_open = os.open
    denied = []

    def open_lock(path, *args, **kwargs):
        if str(path) == lock_path and len(denied) < 2:
            denied.append(path)
            raise PermissionError(13, "lock deletion is still pending", path)
        return real_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", open_lock)
        append_task_track(str(task), {"loss": 1})

    assert len(denied) == 2
    assert not os.path.exists(lock_path)
    append_task_track(str(task), {"loss": 2})
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": [1, 2]}]


@pytest.mark.parametrize("timeout", [0, 0.06, 10])
def test_task_lock_creation_bounds_permission_retries(task, monkeypatch, timeout):
    lock_path = str(task / info_io._LOCK_FILENAME)
    real_open = os.open
    clock = [0.0]
    attempts = []
    error = PermissionError(13, "persistent permission denial", lock_path)

    def open_lock(path, *args, **kwargs):
        if str(path) == lock_path:
            attempts.append(clock[0])
            raise error
        return real_open(path, *args, **kwargs)

    def advance(delay):
        clock[0] += delay

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", open_lock)
        patch.setattr(info_io.time, "monotonic", lambda: clock[0])
        patch.setattr(info_io.time, "sleep", advance)
        patch.setattr(
            info_io, "_release_task_lock",
            lambda *_: pytest.fail("an unacquired lock must not be inspected or released"),
        )
        with pytest.raises(PermissionError) as caught:
            with task_info_lock(str(task), timeout_sec=timeout):
                pytest.fail("must not enter without a lock")

    assert caught.value is error
    assert clock[0] <= timeout
    assert len(attempts) <= info_io._REPLACE_RETRY_COUNT
    if timeout:
        assert len(attempts) > 1
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(append_task_track, str(task), {"loss": 3}).result(timeout=2)
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": [3]}]


def test_task_lock_creation_retry_preserves_a_competing_owner(task, monkeypatch):
    lock_path = task / info_io._LOCK_FILENAME
    real_open = os.open
    owner = f"4242 1 {info_io._LOCK_OWNER_HOST}-other-host 1"
    attempted = False

    def open_lock(path, *args, **kwargs):
        nonlocal attempted
        if str(path) == str(lock_path) and not attempted:
            attempted = True
            lock_path.write_text(owner, encoding="utf-8")
            raise PermissionError(13, "lock deletion is still pending", path)
        return real_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", open_lock)
        with pytest.raises(TimeoutError):
            with task_info_lock(str(task), timeout_sec=0.1):
                pytest.fail("must not steal a competing lock")
    assert lock_path.read_text(encoding="utf-8") == owner


def test_task_lock_release_does_not_remove_new_owner_after_retry(task, monkeypatch):
    lock_path = task / info_io._LOCK_FILENAME
    original_remove = os.remove
    failures = []

    def remove(path, *args, **kwargs):
        if str(path) == str(lock_path):
            if not failures:
                failures.append(path)
                lock_path.write_text("different owner", encoding="utf-8")
                raise PermissionError("owner changed during cleanup")
            pytest.fail("release retried without rechecking ownership")
        return original_remove(path, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "remove", remove)
            with task_info_lock(str(task)):
                pass
        assert lock_path.read_text(encoding="utf-8") == "different owner"
    finally:
        lock_path.unlink(missing_ok=True)


def test_detail_read_recovers_from_writer_lock_without_including_later_append(task, monkeypatch):
    externalize(task)
    append_task_track(str(task), {"loss": 4})
    descriptor = load_task_metadata(str(task), raise_error=True)["track_store"]
    start_writer, locked, busy, released = (threading.Event() for _ in range(4))
    busy_errors = []
    original_connect, original_loads = sqlite3.connect, json.loads

    class ObservedReader(sqlite3.Connection):
        def execute(self, *args, **kwargs):
            try:
                return super().execute(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if str(exc) == "database is locked":
                    busy_errors.append(exc)
                    busy.set()
                    assert released.wait(10), "writer did not release its lock"
                raise

    def connect(*args, **kwargs):
        if "mode=ro" not in str(args[0]):
            return original_connect(*args, **kwargs)
        connection = original_connect(*args, **kwargs, factory=ObservedReader)
        connection.execute("PRAGMA busy_timeout=0")
        return connection

    def decode(text, *args, **kwargs):
        if text == '[{"loss":[0,1,2,3]}]' and not start_writer.is_set():
            start_writer.set()
            assert locked.wait(10), "writer did not acquire its lock"
        return original_loads(text, *args, **kwargs)

    def write():
        try:
            assert start_writer.wait(10)
            with original_connect(task / track_store.TRACK_STORE_FILENAME) as connection:
                connection.execute("BEGIN EXCLUSIVE")
                connection.execute(
                    "INSERT INTO points(generation,operation,run_index,payload) VALUES (?,?,?,?)",
                    (descriptor["generation"], "contending-writer", 1, '{"loss":5}'),
                )
                locked.set()
                assert busy.wait(10), "reader never encountered the writer lock"
        finally:
            released.set()

    monkeypatch.setattr(track_store.sqlite3, "connect", connect)
    monkeypatch.setattr(track_store.json, "loads", decode)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(write)
        try:
            captured = load_task_info(str(task), raise_error=True)
        finally:
            start_writer.set()
            busy.set()
            writer.result(timeout=10)
    assert busy_errors
    assert captured["tracks"] == [{"loss": [0, 1, 2, 3, 4]}]
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": [0, 1, 2, 3, 4, 5]}]


@pytest.mark.parametrize("pause_at", ["snapshot", "point", "bounded batch"])
def test_detail_decode_allows_writers_and_excludes_later_appends(task, monkeypatch, pause_at):
    externalize(task)
    append_task_track(str(task), {"loss": 4})
    expected = [0, 1, 2, 3, 4]
    if pause_at == "bounded batch":
        monkeypatch.setattr(track_store, "_READ_BATCH_CHARS", 1)
        for value in (6, 7):
            append_task_track(str(task), {"loss": value})
            expected.append(value)
    original_loads = json.loads
    target = '[{"loss":[0,1,2,3]}]' if pause_at == "snapshot" else '{"loss":4}'
    decoding = threading.Event()
    resume = threading.Event()

    def decode(text, *args, **kwargs):
        if text == target and not decoding.is_set():
            decoding.set()
            assert resume.wait(10), "reader was not released"
        return original_loads(text, *args, **kwargs)

    monkeypatch.setattr(track_store.json, "loads", decode)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(load_task_info, str(task), True)
        try:
            assert decoding.wait(5), "reader did not reach JSON decoding"
            writer = pool.submit(append_task_track, str(task), {"loss": 5})
            writer.result(timeout=3)
        finally:
            resume.set()
        assert reader.result(timeout=5)["tracks"] == [{"loss": expected}]
    assert load_task_info(str(task), raise_error=True)["tracks"] == [{"loss": expected + [5]}]


def test_replacement_between_read_batches_reloads_complete_new_generation(task, monkeypatch):
    externalize(task)
    for value in range(4, 8):
        append_task_track(str(task), {"loss": value})
    monkeypatch.setattr(track_store, "_READ_BATCH_POINTS", 1)
    original_loads = json.loads
    decoding = threading.Event()
    resume = threading.Event()

    def decode(text, *args, **kwargs):
        if text == '{"loss":4}' and not decoding.is_set():
            decoding.set()
            assert resume.wait(10), "reader was not released"
        return original_loads(text, *args, **kwargs)

    def replace():
        update_task_info(str(task), lambda info: info.update(tracks=[{"loss": [100, 101]}]))
        # Pruning allows SQLite to reuse event sequence numbers. A stale reader
        # must follow the generation identity rather than mix these new rows in.
        append_task_track(str(task), {"loss": 102})

    monkeypatch.setattr(track_store.json, "loads", decode)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(load_task_info, str(task), True)
        try:
            assert decoding.wait(5)
            pool.submit(replace).result(timeout=3)
        finally:
            resume.set()
        assert reader.result(timeout=5)["tracks"] == [{"loss": [100, 101, 102]}]
    with sqlite3.connect(task / track_store.TRACK_STORE_FILENAME) as connection:
        assert connection.execute("SELECT count(*) FROM generations").fetchone()[0] == 1


@pytest.mark.parametrize("replacements", [4, 5], ids=["last-attempt", "exhausted"])
@pytest.mark.parametrize("raise_error", [False, True], ids=["best-effort", "strict"])
def test_detail_read_bounds_retries_when_generations_keep_changing(task, monkeypatch, replacements, raise_error):
    externalize(task)
    original_read = track_store.read_tracks
    calls = 0

    def replace_before_read(task_dir, descriptor, **kwargs):
        nonlocal calls
        calls += 1
        if calls <= replacements:
            info = load_task_metadata(task_dir, raise_error=True)
            info["tracks"] = [{"loss": [calls]}]
            # A real replacement prunes the generation held by this reader.
            save_task_info(task_dir, info)
        return original_read(task_dir, descriptor, **kwargs)

    monkeypatch.setattr(track_store, "read_tracks", replace_before_read)
    if replacements == 5 and raise_error:
        with pytest.raises(track_store.MissingTrackGeneration):
            load_task_info(str(task), raise_error=True)
    else:
        result = load_task_info(str(task), raise_error=raise_error)
        if replacements == 5:
            assert result == {}
        else:
            assert result["tracks"] == [{"loss": [4]}]
    assert calls == 5
