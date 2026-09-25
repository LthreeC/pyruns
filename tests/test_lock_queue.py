"""Task-lock contention, abandoned queue entries, and cleanup boundaries."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys

import pytest

from pyruns.utils import info_io, lock_queue


@pytest.fixture
def task_dir(tmp_path):
    directory = tmp_path / "_pyruns_" / "main" / "tasks" / "alpha"
    directory.mkdir(parents=True)
    return directory


def test_uncontended_task_lock_does_not_create_queue_files(task_dir):
    with info_io.task_info_lock(str(task_dir)):
        assert sorted(path.name for path in task_dir.iterdir()) == [info_io._LOCK_FILENAME]
    assert list(task_dir.iterdir()) == []


def test_waiting_writer_precedes_later_arrivals(task_dir):
    first = lock_queue.TaskLockQueue(str(task_dir))
    second = lock_queue.TaskLockQueue(str(task_dir))
    late = lock_queue.TaskLockQueue(str(task_dir))
    try:
        first.register(5)
        second.register(5)
        assert first.is_first() and not second.is_first()
        first.close()
        late.register(5)
        assert second.is_first() and not late.is_first()
        second.close()
        assert late.is_first()
    finally:
        first.close()
        second.close()
        late.close()


def test_task_lock_timeout_preserves_an_earlier_waiter(task_dir):
    earlier = lock_queue.TaskLockQueue(str(task_dir))
    earlier.register(5)
    owned_path = Path(earlier.entry.path)
    try:
        with pytest.raises(TimeoutError):
            with info_io.task_info_lock(str(task_dir), timeout_sec=0.02):
                pytest.fail("a later writer bypassed the waiting writer")
        assert list(owned_path.parent.iterdir()) == [owned_path]
    finally:
        earlier.close()
    with info_io.task_info_lock(str(task_dir), timeout_sec=0):
        pass


@pytest.mark.parametrize("failure", ["register", "body"])
def test_queued_task_lock_failure_releases_thread_and_file_locks(task_dir, monkeypatch, failure):
    real_open = os.open

    def failing_open(path, *args, **kwargs):
        if str(path).endswith(".wait"):
            raise PermissionError("cannot create waiter")
        return real_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        # The previous waiter can leave between detection and registration.
        patch.setattr(lock_queue.TaskLockQueue, "has_waiters", lambda self: True)
        if failure == "register":
            patch.setattr(os, "open", failing_open)
        with pytest.raises((PermissionError, RuntimeError)):
            with info_io.task_info_lock(str(task_dir)):
                raise RuntimeError("update failed")
    assert not (task_dir / info_io._LOCK_FILENAME).exists()
    assert not list(task_dir.glob("**/*.wait"))

    def acquire_again():
        with info_io.task_info_lock(str(task_dir), timeout_sec=0.1):
            return True

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(acquire_again).result(timeout=2)


@pytest.mark.parametrize("reason", ["exited", "reused_pid", "expired"])
def test_abandoned_waiter_does_not_block_task_updates(task_dir, monkeypatch, reason):
    waiter = lock_queue.TaskLockQueue(str(task_dir))
    waiter.register(5)
    owned_path = Path(waiter.entry.path)
    if reason == "exited":
        monkeypatch.setattr(lock_queue, "is_pid_running", lambda pid: False)
    elif reason == "reused_pid":
        monkeypatch.setattr(lock_queue, "get_process_create_time", lambda pid: waiter.entry.created_us / 1_000_000 + 1)
    else:
        monkeypatch.setattr(lock_queue.time, "time_ns", lambda: waiter.entry.expires_ns + 1)
    with info_io.task_info_lock(str(task_dir), timeout_sec=0.05):
        assert not owned_path.exists()


def test_remote_waiter_is_kept_until_its_expiry(task_dir, monkeypatch):
    waiter = lock_queue.TaskLockQueue(str(task_dir))
    waiter.register(5)
    owned_path = Path(waiter.entry.path)
    monkeypatch.setattr(lock_queue, "_HOST", "0" * 16 if lock_queue._HOST != "0" * 16 else "1" * 16)
    monkeypatch.setattr(lock_queue, "is_pid_running", lambda pid: False)
    with pytest.raises(TimeoutError):
        with info_io.task_info_lock(str(task_dir), timeout_sec=0):
            pytest.fail("remote writer was treated as a dead local process")
    assert owned_path.exists()
    monkeypatch.setattr(lock_queue.time, "time_ns", lambda: waiter.entry.expires_ns + 1)
    with info_io.task_info_lock(str(task_dir), timeout_sec=0):
        assert not owned_path.exists()


def test_renamed_task_discards_waiters_for_the_old_name(task_dir):
    waiter = lock_queue.TaskLockQueue(str(task_dir))
    waiter.register(5)
    renamed = task_dir.with_name("beta")
    task_dir.rename(renamed)
    with info_io.task_info_lock(str(renamed), timeout_sec=0):
        assert not list(renamed.glob("**/*.wait"))
    waiter.close()


def test_waiter_cleanup_preserves_a_replacement_file(task_dir):
    waiter = lock_queue.TaskLockQueue(str(task_dir))
    waiter.register(5)
    path = Path(waiter.entry.path)
    path.rename(path.with_suffix(".displaced"))
    path.write_text("replacement", encoding="utf-8")
    waiter.close()
    assert path.read_text(encoding="utf-8") == "replacement"


def test_waiter_cleanup_recovers_from_temporary_sharing_error(task_dir, monkeypatch):
    waiter = lock_queue.TaskLockQueue(str(task_dir))
    waiter.register(5)
    path = Path(waiter.entry.path)
    real_unlink = os.unlink
    denied = []

    def deny_once(candidate, *args, **kwargs):
        if Path(candidate) == path and not denied:
            denied.append(candidate)
            raise PermissionError("temporary sharing violation")
        return real_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", deny_once)
    waiter.close()
    assert denied and not path.exists()
    with info_io.task_info_lock(str(task_dir), timeout_sec=0):
        pass


def test_queue_cleanup_exception_still_releases_the_thread_lock(task_dir, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(lock_queue.TaskLockQueue, "close", lambda self:
                      (_ for _ in ()).throw(RuntimeError("cleanup failed")))
        with pytest.raises(RuntimeError, match="cleanup failed"):
            with info_io.task_info_lock(str(task_dir)):
                pass

    def acquire_again():
        with info_io.task_info_lock(str(task_dir), timeout_sec=0.1):
            return True

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(acquire_again).result(timeout=2)


def test_missing_waiter_cannot_claim_the_task_lock(task_dir):
    waiter = lock_queue.TaskLockQueue(str(task_dir))
    waiter.register(5)
    Path(waiter.entry.path).unlink()
    with pytest.raises(FileNotFoundError, match="reservation disappeared"):
        waiter.is_first()


def test_waiter_directory_reparse_is_rejected_before_use(task_dir, monkeypatch):
    directory = task_dir / lock_queue._QUEUE_DIR
    directory.mkdir()
    real_check = info_io._path_is_link_or_reparse
    monkeypatch.setattr(info_io, "_path_is_link_or_reparse", lambda path:
                        os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(directory)) or real_check(path))
    with pytest.raises(ValueError, match="reparse point"):
        with info_io.task_info_lock(str(task_dir)):
            pytest.fail("must reject a redirected waiter directory")
    assert not (task_dir / info_io._LOCK_FILENAME).exists()


def test_waiter_is_recovered_after_real_process_exit(task_dir):
    result = subprocess.run(
        [sys.executable, "-c", "import os,sys; from pyruns.utils.lock_queue import TaskLockQueue; "
         "queue=TaskLockQueue(sys.argv[1]); queue.register(60); os._exit(23)", str(task_dir)],
        capture_output=True, timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 23, result.stderr.decode("utf-8", errors="replace")
    assert list(task_dir.glob("**/*.wait"))
    with info_io.task_info_lock(str(task_dir), timeout_sec=0.5):
        assert not list(task_dir.glob("**/*.wait"))
