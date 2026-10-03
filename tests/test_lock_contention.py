"""File-lock initialization, ownership, and sharing-error recovery."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import pyruns.core.task_generator as task_generator_module
from pyruns.core.task_generator import TaskGenerator
from pyruns.utils import info_io, settings


def _probe_task_lock(task):
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
from pyruns.utils.info_io import task_info_lock
try:
    with task_info_lock(sys.argv[1], timeout_sec=0.05):
        print("acquired")
except TimeoutError:
    print("blocked")
""", str(task)],
        capture_output=True, text=True, timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_task_lock_release_keeps_waiters_out_until_unlink(tmp_path, monkeypatch):
    task = tmp_path / "tasks" / "sample"
    task.mkdir(parents=True)
    lock_path = task / info_io._LOCK_FILENAME
    real_remove = os.remove
    observations = []

    def contend_before_unlink(path, *args, **kwargs):
        if Path(path) == lock_path:
            observations.append(_probe_task_lock(task))
        return real_remove(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(info_io.os, "remove", contend_before_unlink)
        with info_io.task_info_lock(str(task)):
            pass
    assert observations == ["blocked"]
    with info_io.task_info_lock(str(task), timeout_sec=0):
        pass


def test_task_lock_release_never_unlinks_after_publishing_reclaimable_marker(tmp_path, monkeypatch):
    task = tmp_path / "tasks" / "sample"
    task.mkdir(parents=True)
    lock_path = task / info_io._LOCK_FILENAME
    original_remove = os.remove
    original_mark = info_io._mark_task_lock_released
    attempts = []
    replacement = "4242 1 another-host 1"

    def deny_delete(path, *args, **kwargs):
        if Path(path) == lock_path:
            attempts.append(lock_path.read_text(encoding="utf-8"))
            raise PermissionError("Windows sharing violation")
        return original_remove(path, *args, **kwargs)

    def reclaim_after_marker(fd, owner):
        result = original_mark(fd, owner)
        assert info_io._lock_file_is_stale(str(lock_path))
        # A competing process can now replace the completed owner's file.
        # Replace content here because Windows may still hold this descriptor.
        lock_path.write_text(replacement, encoding="utf-8")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(info_io, "_REPLACE_RETRY_COUNT", 2)
        patch.setattr(info_io, "_REPLACE_RETRY_DELAY_SEC", 0)
        patch.setattr(info_io.os, "remove", deny_delete)
        patch.setattr(info_io, "_mark_task_lock_released", reclaim_after_marker)
        with info_io.task_info_lock(str(task)):
            pass
    assert len(attempts) == 2
    assert all(not owner.endswith(" released") for owner in attempts)
    assert lock_path.read_text(encoding="utf-8") == replacement


def test_task_lock_stale_reapers_cannot_overlap_acquisition(tmp_path, monkeypatch):
    task = tmp_path / "tasks" / "sample"
    task.mkdir(parents=True)
    lock_path = task / info_io._LOCK_FILENAME
    lock_path.write_text(f"0 1 some-host 1 {info_io._LOCK_PROTOCOL} released", encoding="utf-8")
    original_replace = os.replace
    observations = []

    def contend_before_quarantine(src, dst):
        if Path(src) == lock_path:
            observations.append(_probe_task_lock(task))
        return original_replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(info_io.os, "replace", contend_before_quarantine)
        with info_io.task_info_lock(str(task)):
            pass
    assert observations == ["blocked"]
    assert _probe_task_lock(task) == "acquired"


def test_task_lock_guard_recovers_after_holder_is_killed(tmp_path):
    task = tmp_path / "tasks" / "sample"
    task.mkdir(parents=True)
    ready = tmp_path / "ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", """
import sys, time
from pathlib import Path
from pyruns.utils.info_io import task_info_lock
task, ready = map(Path, sys.argv[1:])
with task_info_lock(str(task)):
    ready.touch()
    time.sleep(60)
""", str(task), str(ready)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "child did not acquire the task lock"
        guard_path = task / info_io._LOCK_GUARD_FILENAME
        guard_identity = guard_path.stat().st_ino
        assert _probe_task_lock(task) == "blocked"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=15)
    # The same runtime's native guard proves this tagged marker is abandoned.
    assert _probe_task_lock(task) == "acquired"
    assert guard_path.stat().st_ino == guard_identity
    assert not (task / info_io._LOCK_FILENAME).exists()


def test_task_lock_refuses_unsafe_fallback_when_native_locking_is_unsupported(tmp_path, monkeypatch):
    from pyruns.update_coordination import CoordinationStore

    monkeypatch.setattr(CoordinationStore, "_try_native_lock", staticmethod(lambda fd: None))
    with pytest.raises(OSError, match="Enable file locking"):
        with info_io.task_info_lock(str(tmp_path)):
            pytest.fail("unsupported native locking must not permit an update")
    assert not (tmp_path / info_io._LOCK_FILENAME).exists()


@pytest.mark.parametrize("owner", [
    "", "0 0", "999999999 1 same-host 1", "999999999 1 same-host 1 released",
    "0 0 same-host 1 guard-v2:another-domain", "0 0 same-host 1 guard-v2:another-domain released",
])
def test_task_lock_keeps_unknown_or_foreign_lock_domains(tmp_path, owner):
    lock_path = tmp_path / info_io._LOCK_FILENAME
    lock_path.write_text(owner, encoding="utf-8")
    with pytest.raises(TimeoutError, match="original runtime"):
        with info_io.task_info_lock(str(tmp_path), timeout_sec=0.02):
            pytest.fail("another runtime's PID cannot prove its owner exited")
    assert lock_path.read_text(encoding="utf-8") == owner


def test_task_lock_rejects_redirected_guard(tmp_path, simulate_reparse):
    guard = tmp_path / info_io._LOCK_GUARD_FILENAME
    guard.write_text("keep", encoding="utf-8")
    simulate_reparse(guard)
    with pytest.raises(ValueError, match="Task lock guard must not be"):
        with info_io.task_info_lock(str(tmp_path)):
            pytest.fail("redirected guard must not grant access")
    assert guard.read_text(encoding="utf-8") == "keep"


@pytest.fixture(params=["task", "settings"])
def initializing_lock(request, tmp_path):
    if request.param == "task":
        task = tmp_path / "tasks" / "sample"
        task.mkdir(parents=True)
        return lambda: info_io.task_info_lock(str(task), timeout_sec=0), task / info_io._LOCK_FILENAME

    settings_path = tmp_path / "settings.yaml"

    @contextmanager
    def acquire():
        fd, lock_path, owner = settings._open_settings_lock(str(settings_path), timeout_sec=0)
        try:
            yield
        finally:
            os.close(fd)
            settings._release_settings_lock(lock_path, owner)

    return acquire, Path(f"{settings_path}.lock")


@pytest.mark.parametrize("failure", ["short", "interrupt"])
def test_lock_owner_initialization_failure_closes_and_releases(initializing_lock, monkeypatch, failure):
    acquire, lock_path = initializing_lock
    original_write = os.write
    owner_fd = None

    def incomplete_write(fd, data):
        nonlocal owner_fd
        owner_fd = fd
        written = original_write(fd, data[:-1])
        if failure == "interrupt":
            raise KeyboardInterrupt("lock owner interrupted")
        return written

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", incomplete_write)
        with pytest.raises(OSError if failure == "short" else KeyboardInterrupt):
            with acquire():
                pytest.fail("must not enter with an incomplete lock owner")
    assert owner_fd is not None
    try:
        os.fstat(owner_fd)
    except OSError as exc:
        assert exc.errno == errno.EBADF
    else:
        os.close(owner_fd)
        pytest.fail("failed lock initialization leaked its descriptor")
    assert not lock_path.exists()
    with acquire():
        pass


def test_lock_initialization_cleanup_keeps_a_replacement_owner(initializing_lock, monkeypatch):
    acquire, lock_path = initializing_lock
    original_close = os.close
    owner_fd = None
    replacement = b"new owner acquired after the failed initializer closed"
    replaced = False

    def fail_write(fd, data):
        nonlocal owner_fd
        owner_fd = fd
        raise OSError("cannot initialize lock owner")

    def replace_after_close(fd):
        nonlocal replaced
        original_close(fd)
        if fd == owner_fd and not replaced:
            replaced = True
            lock_path.rename(lock_path.with_suffix(".displaced"))
            lock_path.write_bytes(replacement)

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", fail_write)
        patch.setattr(os, "close", replace_after_close)
        with pytest.raises(OSError, match="cannot initialize lock owner"):
            with acquire():
                pytest.fail("failed initialization entered the protected body")
    assert replaced
    assert lock_path.read_bytes() == replacement


@pytest.mark.parametrize("failure_at", ["identity", "unlink"])
def test_task_name_release_recovers_temporary_sharing_error(tmp_path, monkeypatch, failure_at):
    generator = TaskGenerator(str(tmp_path / "tasks"))
    reservation = generator.reserve_exact_task_name("alpha")
    assert reservation is not None
    lock_path = Path(reservation[0])
    original = generator._path_identity if failure_at == "identity" else os.unlink
    failures = 0

    def fail_once(path, *args, **kwargs):
        nonlocal failures
        if Path(path) == lock_path and failures == 0:
            failures += 1
            raise PermissionError("temporary sharing violation")
        return original(path, *args, **kwargs)

    if failure_at == "identity":
        monkeypatch.setattr(generator, "_path_identity", fail_once)
    else:
        monkeypatch.setattr(task_generator_module.os, "unlink", fail_once)
    generator.release_task_name_reservation(reservation)
    assert failures == 1
    assert not lock_path.exists()
    next_reservation = generator.reserve_exact_task_name("alpha")
    assert next_reservation is not None
    generator.release_task_name_reservation(next_reservation)


@pytest.mark.parametrize("failure_at", ["read", "delete"])
def test_settings_release_recovers_temporary_sharing_error(tmp_path, monkeypatch, failure_at):
    settings_path = tmp_path / "settings.yaml"
    fd, lock_name, owner = settings._open_settings_lock(str(settings_path))
    os.close(fd)
    lock_path = Path(lock_name)
    original_open = open
    original_replace = os.replace
    original_remove = os.remove
    failures = 0

    def fail_read_once(path, *args, **kwargs):
        nonlocal failures
        if Path(path) == lock_path and failures == 0:
            failures += 1
            raise PermissionError("temporary read sharing violation")
        return original_open(path, *args, **kwargs)

    def deny_deletion_once(operation, path, *args, **kwargs):
        nonlocal failures
        # A held Windows reader can deny both rename and the unlink fallback.
        if Path(path) == lock_path and failures < 2:
            failures += 1
            raise PermissionError("temporary delete sharing violation")
        return operation(path, *args, **kwargs)

    if failure_at == "read":
        monkeypatch.setattr(settings, "open", fail_read_once, raising=False)
    else:
        monkeypatch.setattr(settings.os, "replace", lambda *args, **kwargs: deny_deletion_once(original_replace, *args, **kwargs))
        monkeypatch.setattr(settings.os, "remove", lambda *args, **kwargs: deny_deletion_once(original_remove, *args, **kwargs))
    settings._release_settings_lock(lock_name, owner)
    assert failures == (1 if failure_at == "read" else 2)
    assert not lock_path.exists()
    next_fd, next_path, next_owner = settings._open_settings_lock(str(settings_path), timeout_sec=0)
    os.close(next_fd)
    settings._release_settings_lock(next_path, next_owner)


def test_task_name_release_keeps_a_replacement_owner_during_retry(tmp_path, monkeypatch):
    generator = TaskGenerator(str(tmp_path / "tasks"))
    reservation = generator.reserve_exact_task_name("alpha")
    assert reservation is not None
    lock_path = Path(reservation[0])
    original_unlink = os.unlink
    replacement = b"another creator owns this name"
    replaced = False

    def replace_then_deny(path, *args, **kwargs):
        nonlocal replaced
        if Path(path) == lock_path and not replaced:
            replaced = True
            lock_path.rename(lock_path.with_suffix(".displaced"))
            lock_path.write_bytes(replacement)
            raise PermissionError("owner changed during sharing violation")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(task_generator_module.os, "unlink", replace_then_deny)
    generator.release_task_name_reservation(reservation)
    assert replaced
    assert lock_path.read_bytes() == replacement


def test_settings_release_keeps_a_replacement_owner_during_retry(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.yaml"
    fd, lock_name, owner = settings._open_settings_lock(str(settings_path))
    os.close(fd)
    lock_path = Path(lock_name)
    replacement = settings._settings_lock_owner_bytes()
    assert replacement != owner
    original_open = open
    replaced = False

    def replace_then_deny(path, *args, **kwargs):
        nonlocal replaced
        if Path(path) == lock_path and not replaced:
            replaced = True
            lock_path.rename(lock_path.with_suffix(".displaced"))
            lock_path.write_bytes(replacement)
            raise PermissionError("owner changed during sharing violation")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(settings, "open", replace_then_deny, raising=False)
    settings._release_settings_lock(lock_name, owner)
    assert replaced
    assert lock_path.read_bytes() == replacement


@pytest.mark.parametrize("kind", ["task_name", "settings_save", "settings_unset"])
def test_lock_release_has_a_bounded_retry_when_access_stays_denied(tmp_path, monkeypatch, kind):
    if kind == "task_name":
        generator = TaskGenerator(str(tmp_path / "tasks"))
        reservation = generator.reserve_exact_task_name("alpha")
        lock_path = Path(reservation[0])
        release = lambda: generator.release_task_name_reservation(reservation)
        original = os.unlink
        parse_owner = generator._task_name_lock_owner
    else:
        settings.save_setting_for_root(str(tmp_path), "header_refresh_interval", 3.0)
        lock_path = Path(settings._settings_path(str(tmp_path)) + ".lock")
        release = (
            lambda: settings.save_setting_for_root(str(tmp_path), "header_refresh_interval", 4.0)
        ) if kind == "settings_save" else (
            lambda: settings.unset_setting_for_root(str(tmp_path), "header_refresh_interval")
        )
        original = open
        parse_owner = settings._settings_lock_owner
    attempts = 0

    def deny_access(path, *args, **kwargs):
        nonlocal attempts
        if Path(path) == lock_path:
            attempts += 1
            assert attempts <= 20, "lock release retry must be bounded"
            raise PermissionError("persistent access denial")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        if kind == "task_name":
            patch.setattr(task_generator_module.os, "unlink", deny_access)
        else:
            patch.setattr(settings, "open", deny_access, raising=False)
        release()
    assert 1 <= attempts <= 20
    assert parse_owner(lock_path.read_bytes())["pid"] == os.getpid()
    if kind == "task_name":
        next_reservation = generator.reserve_exact_task_name("alpha")
        assert next_reservation is not None
        generator.release_task_name_reservation(next_reservation)
    else:
        settings.save_setting_for_root(str(tmp_path), "header_refresh_interval", 6.0)
        assert settings.load_settings(str(tmp_path))["header_refresh_interval"] == 6.0
    assert not lock_path.exists()
