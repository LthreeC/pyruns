"""Bounded binary reads shared by configuration and metadata loaders."""

from __future__ import annotations

import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from typing import BinaryIO, Callable, TypeVar


_READ_CHUNK_BYTES = 64 * 1024
_Item = TypeVar("_Item")
_Result = TypeVar("_Result")


def _parallel_loading_worthwhile(elapsed: float, cpu_time: float) -> bool:
    return elapsed >= 0.008 and elapsed - cpu_time >= elapsed * 0.5


def map_disk_reads(items: list[_Item], load: Callable[[_Item], _Result]) -> list[_Result]:
    """Preserve order and use bounded concurrency on consistently slow storage."""
    if len(items) <= 8:
        return [load(item) for item in items]

    results = []
    waiting_samples = 0
    for index in range(3):
        started = time.perf_counter()
        cpu_started = time.thread_time()
        results.append(load(items[index]))
        if _parallel_loading_worthwhile(time.perf_counter() - started, time.thread_time() - cpu_started):
            waiting_samples += 1
        # One slow read may be an outlier, especially on an otherwise local disk.
        if waiting_samples >= 2:
            remaining = items[index + 1:]
            with ThreadPoolExecutor(max_workers=min(16, len(remaining))) as pool:
                results.extend(pool.map(load, remaining))
            return results

    results.extend(load(item) for item in items[3:])
    return results


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
