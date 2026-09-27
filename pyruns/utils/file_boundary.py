"""Check the opened object before reading a file from a task directory."""

from __future__ import annotations

import os
import stat
import sys
from functools import lru_cache
from typing import BinaryIO


@lru_cache(maxsize=1)
def _windows_path_function():
    import ctypes
    from ctypes import wintypes

    function = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    function.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    function.restype = wintypes.DWORD
    return function


def _opened_file_path(fd: int) -> str | None:
    if os.name == "nt":
        import ctypes
        import msvcrt

        function = _windows_path_function()
        size = 512
        while True:
            buffer = ctypes.create_unicode_buffer(size)
            length = function(msvcrt.get_osfhandle(fd), buffer, size, 0)
            if not length:
                raise ctypes.WinError(ctypes.get_last_error())
            if length < size:
                return buffer.value
            size = length + 1
    try:
        if sys.platform.startswith("linux"):
            return os.readlink(f"/proc/self/fd/{fd}")
        if sys.platform == "darwin":
            import fcntl

            # Darwin F_GETPATH writes at most MAXPATHLEN (1024) bytes.
            return os.fsdecode(fcntl.fcntl(fd, 50, b"\0" * 1024).split(b"\0", 1)[0])
    except OSError:
        pass
    return None


def _is_within(path: str, boundary: str) -> bool:
    # Windows handle paths use the extended namespace. Preserve literal dots
    # and spaces by giving the already-resolved boundary the same spelling.
    if os.name == "nt" and not boundary.startswith("\\\\?\\"):
        boundary = "\\\\?\\UNC\\" + boundary[2:] if boundary.startswith("\\\\") else "\\\\?\\" + boundary
    try:
        return os.path.isabs(path) and os.path.normcase(os.path.commonpath([path, boundary])) == os.path.normcase(boundary)
    except ValueError:
        return False


def _contained_file_stat(path: str, boundary: str) -> os.stat_result:
    """Resolve contained links, then open every real component without links.

    This slower POSIX fallback does not depend on /proc. Comparing identities
    binds the caller's descriptor to this walk without reading either file.
    """
    resolved = os.path.realpath(path)
    if not _is_within(resolved, boundary):
        raise ValueError(f"Task file resolves outside its workspace boundary: {path}")
    parts = resolved.strip(os.sep).split(os.sep)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open(os.sep, directory_flags)
    try:
        for component in parts[:-1]:
            child = os.open(component, directory_flags, dir_fd=current)
            os.close(current)
            current = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        try:
            return os.fstat(fd)
        finally:
            os.close(fd)
    finally:
        os.close(current)


def validate_open_file(handle: BinaryIO, path: str, boundary: str) -> None:
    """Reject an opened object outside the frozen, already-resolved boundary."""
    fd = handle.fileno()
    opened = _opened_file_path(fd)
    if opened is not None:
        contained = _is_within(opened, boundary)
    else:
        expected = _contained_file_stat(path, boundary)
        contained = stat.S_ISREG(expected.st_mode) and os.path.samestat(expected, os.fstat(fd))
    if not contained:
        raise ValueError(f"Task file resolves outside its workspace boundary: {path}")
