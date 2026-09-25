"""Sharing-error recovery must release only the caller's own file locks."""
import json
import os
from pathlib import Path

import pytest

import pyruns.core.task_generator as task_generator_module
from pyruns.core.task_generator import TaskGenerator
from pyruns.utils import settings


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


@pytest.mark.parametrize("kind", ["task_name", "settings"])
def test_lock_release_has_a_bounded_retry_when_access_stays_denied(tmp_path, monkeypatch, kind):
    if kind == "task_name":
        generator = TaskGenerator(str(tmp_path / "tasks"))
        reservation = generator.reserve_exact_task_name("alpha")
        lock_path = Path(reservation[0])
        release = lambda: generator.release_task_name_reservation(reservation)
        original = os.unlink
    else:
        fd, lock_name, owner = settings._open_settings_lock(str(tmp_path / "settings.yaml"))
        os.close(fd)
        lock_path = Path(lock_name)
        release = lambda: settings._release_settings_lock(lock_name, owner)
        original = open
    original_owner = lock_path.read_bytes()
    attempts = 0

    def deny_access(path, *args, **kwargs):
        nonlocal attempts
        if Path(path) == lock_path:
            attempts += 1
            assert attempts <= 20, "lock release retry must be bounded"
            raise PermissionError("persistent access denial")
        return original(path, *args, **kwargs)

    if kind == "task_name":
        monkeypatch.setattr(task_generator_module.os, "unlink", deny_access)
    else:
        monkeypatch.setattr(settings, "open", deny_access, raising=False)
    release()
    assert 1 <= attempts <= 20
    assert lock_path.read_bytes() == original_owner
    assert json.loads(original_owner)["pid"] == os.getpid()
