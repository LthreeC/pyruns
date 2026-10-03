"""Permanent native guards shared by file-backed write protocols."""

from __future__ import annotations

import hashlib
import os
import socket
import sys
import time

from pyruns.update_coordination import CoordinationStore
from pyruns.utils.file_boundary import validate_open_file
from pyruns.utils.file_io import regular_file_opener


_DOMAIN = hashlib.blake2b(
    "\0".join((os.name, sys.platform, socket.gethostname().lower(), os.getenv("WSL_DISTRO_NAME", ""))).encode("utf-8"),
    digest_size=16,
).hexdigest()
LOCK_PROTOCOL = f"guard-v2:{_DOMAIN}"


class NativeFileGuard:
    """Keep the same inode locked; never remove its permanent guard file."""

    def __init__(self, path: str, boundary: str, *, label: str, timeout_sec: float = 5.0):
        self.path = path
        self.boundary = boundary
        self.label = label
        self.timeout_sec = timeout_sec
        self.fd: int | None = None
        self.acquired = False

    def try_acquire(self) -> bool:
        from pyruns.utils.info_io import validate_workspace_file

        if self.acquired:
            return True
        if self.fd is None:
            validate_workspace_file(self.path, self.boundary, label=self.label)
            boundary = os.path.realpath(self.boundary)
            self.fd = regular_file_opener(
                self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                with os.fdopen(self.fd, "rb", closefd=False) as handle:
                    validate_open_file(handle, self.path, boundary)
            except BaseException:
                self.close()
                raise
        acquired = CoordinationStore._try_native_lock(self.fd)
        if acquired is None:
            raise OSError(
                f"{self.label} requires native file locking: {self.boundary}. "
                "Enable file locking on the shared filesystem or use a local workspace."
            )
        self.acquired = acquired
        return acquired

    def acquire(self) -> None:
        deadline = time.monotonic() + max(0.0, self.timeout_sec)
        try:
            while not self.try_acquire():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Timed out acquiring {self.label.lower()}: {self.path}")
                time.sleep(min(0.005, remaining))
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        fd, acquired = self.fd, self.acquired
        self.fd, self.acquired = None, False
        if fd is not None:
            CoordinationStore._close_native_lock(fd, acquired)

    def __enter__(self) -> "NativeFileGuard":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
