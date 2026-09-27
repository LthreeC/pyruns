"""Bounded binary reads shared by configuration and metadata loaders."""

from __future__ import annotations

import os
import stat
from typing import BinaryIO


_READ_CHUNK_BYTES = 64 * 1024


def regular_file_opener(path: str, flags: int) -> int:
    """Open a document without waiting for FIFO writers, then check its type."""
    fd = os.open(path, flags | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"Document must be a regular file: {path}")
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_bounded_bytes(handle: BinaryIO, limit: int) -> bytes:
    """Read up to limit bytes without reserving the limit for a tiny file.

    The open file's size only guides the first allocation. Continue through
    short reads and file growth, always enforcing the actual byte limit.
    """
    if limit < 0:
        raise ValueError("Read limit must be non-negative")
    if limit == 0:
        return b""

    read_size = min(limit, _READ_CHUNK_BYTES)
    try:
        info = os.fstat(handle.fileno())
        if stat.S_ISREG(info.st_mode):
            read_size = min(limit, max(1, info.st_size - handle.tell() + 1))
    except (AttributeError, OSError, ValueError):
        pass

    chunks = []
    remaining = limit
    while remaining > 0:
        chunk = handle.read(min(read_size, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
        read_size = min(_READ_CHUNK_BYTES, read_size * 2)
    return chunks[0] if len(chunks) == 1 else b"".join(chunks)
