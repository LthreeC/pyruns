"""Order contending task writers without granting access to task data.

The exclusive task lock remains the authority for writes. Queue entries are
short-lived hints, published atomically as empty files with immutable names.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import socket
import stat
import time
from dataclasses import dataclass

from pyruns.utils.process_utils import get_process_create_time, is_pid_running

_QUEUE_DIR = ".pyruns-lock-waiters"
_ENTRY_PATTERN = re.compile(
    r"(\d{20})-([0-9a-f]{16})-(\d+)-(\d+)-(\d+)-([0-9a-f]{16})-([0-9a-f]{16})\.wait\Z"
)
_HOST = hashlib.blake2b(socket.gethostname().lower().encode("utf-8"), digest_size=8).hexdigest()
_EXPIRY_GRACE_SEC = 1.0


def _task_key(task_dir: str) -> str:
    name = os.path.normcase(os.path.basename(os.path.abspath(task_dir)))
    return hashlib.blake2b(os.fsencode(name), digest_size=8).hexdigest()


@dataclass(frozen=True)
class _Entry:
    path: str
    name: str
    sequence: int
    host: str
    pid: int
    created_us: int
    expires_ns: int
    task_key: str
    identity: tuple[int, int]


def _validate_directory(task_dir: str, directory: str) -> None:
    from pyruns.utils.info_io import validate_task_directory, validate_workspace_directory

    validate_task_directory(task_dir)
    validate_workspace_directory(directory)


def _entries(task_dir: str) -> list[_Entry]:
    directory = os.path.join(task_dir, _QUEUE_DIR)
    if not os.path.lexists(directory):
        return []
    _validate_directory(task_dir, directory)
    result = []
    try:
        with os.scandir(directory) as children:
            for child in children:
                match = _ENTRY_PATTERN.fullmatch(child.name)
                if match is None:
                    continue
                try:
                    # Windows DirEntry.stat may omit the inode/device fields.
                    info = os.lstat(child.path)
                except FileNotFoundError:
                    continue
                attributes = int(getattr(info, "st_file_attributes", 0) or 0)
                if not stat.S_ISREG(info.st_mode) or attributes & 0x400:
                    raise ValueError(f"Task lock waiter must be a regular file: {child.path}")
                sequence, host, pid, created, expires, task_key, _token = match.groups()
                result.append(_Entry(
                    child.path, child.name, int(sequence), host, int(pid), int(created),
                    int(expires), task_key, (info.st_dev, info.st_ino),
                ))
    except FileNotFoundError:
        return []
    return result


def _expired(entry: _Entry, task_dir: str) -> bool:
    if entry.task_key != _task_key(task_dir) or time.time_ns() >= entry.expires_ns:
        return True
    if entry.host != _HOST:
        return False
    if not is_pid_running(entry.pid):
        return True
    actual = get_process_create_time(entry.pid) if entry.created_us else None
    return actual is not None and abs(actual * 1_000_000 - entry.created_us) > 10_000


def _discard(task_dir: str, entry: _Entry, *, retry: bool = False) -> None:
    from pyruns.utils.info_io import _REPLACE_RETRY_COUNT, _REPLACE_RETRY_DELAY_SEC

    attempts = _REPLACE_RETRY_COUNT if retry else 1
    for attempt in range(attempts):
        try:
            _validate_directory(task_dir, os.path.dirname(entry.path))
            current = os.lstat(entry.path)
            if (current.st_dev, current.st_ino) == entry.identity:
                os.unlink(entry.path)
            return
        except PermissionError:
            if attempt < attempts - 1:
                time.sleep(_REPLACE_RETRY_DELAY_SEC * (attempt + 1))
        except (OSError, ValueError):
            # An expired hint cannot authorize writes or block the real lock.
            return


class TaskLockQueue:
    """Register only contending writers, preserving the uncontended fast path."""

    def __init__(self, task_dir: str):
        self.task_dir = os.path.abspath(task_dir)
        self.entry: _Entry | None = None

    def has_waiters(self) -> bool:
        found = False
        for entry in _entries(self.task_dir):
            if _expired(entry, self.task_dir):
                _discard(self.task_dir, entry)
            else:
                found = True
        return found

    def register(self, remaining: float) -> None:
        entries = _entries(self.task_dir)
        sequence = max((entry.sequence for entry in entries), default=0) + 1
        if sequence >= 10**20:
            raise ValueError("Task lock queue sequence is out of range")
        directory = os.path.join(self.task_dir, _QUEUE_DIR)
        _validate_directory(self.task_dir, directory)
        os.makedirs(directory, exist_ok=True)
        _validate_directory(self.task_dir, directory)
        pid = os.getpid()
        created = get_process_create_time(pid)
        created_us = int(created * 1_000_000) if created is not None else 0
        expires_ns = time.time_ns() + int((max(0.0, remaining) + _EXPIRY_GRACE_SEC) * 1_000_000_000)
        task_key = _task_key(self.task_dir)
        name = f"{sequence:020d}-{_HOST}-{pid}-{created_us}-{expires_ns}-{task_key}-{secrets.token_hex(8)}.wait"
        path = os.path.join(directory, name)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            info = os.fstat(fd)
            self.entry = _Entry(
                path, name, sequence, _HOST, pid, created_us, expires_ns, task_key,
                (info.st_dev, info.st_ino),
            )
        finally:
            os.close(fd)

    def is_first(self) -> bool:
        if self.entry is None:
            return True
        entries = _entries(self.task_dir)
        if not any(entry.name == self.entry.name and entry.identity == self.entry.identity for entry in entries):
            raise FileNotFoundError("Task lock queue reservation disappeared")
        own_key = self.entry.sequence, self.entry.name
        for entry in entries:
            if entry.name == self.entry.name:
                continue
            if _expired(entry, self.task_dir):
                _discard(self.task_dir, entry)
            elif (entry.sequence, entry.name) < own_key:
                return False
        return True

    def close(self) -> None:
        if self.entry is not None:
            _discard(self.task_dir, self.entry, retry=True)
            self.entry = None
