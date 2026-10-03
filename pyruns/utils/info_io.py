"""Task-level I/O helpers shared by core, UI, and public APIs."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import socket
import stat
import tempfile
import threading
import time
from concurrent.futures import CancelledError
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Callable, Dict, Optional
from weakref import WeakValueDictionary

from pyruns._config import (
    DEFAULT_ROOT_NAME,
    ERROR_LOG_FILENAME,
    QUEUE_LOG_FILENAME,
    RUN_LOGS_DIR,
    SCRIPT_INFO_FILENAME,
    TASK_INFO_FILENAME,
)
from pyruns.utils.file_io import read_bounded_bytes, regular_file_opener
from pyruns.utils.file_boundary import validate_open_file
from pyruns.utils.native_lock import LOCK_PROTOCOL as _LOCK_PROTOCOL, NativeFileGuard

# Active holders and waiters keep strong references; idle task paths can retire.
_TASK_FILE_LOCKS: WeakValueDictionary[str, threading.RLock] = WeakValueDictionary()
_TASK_FILE_LOCKS_GUARD = threading.Lock()
_LOCK_FILENAME = f".{TASK_INFO_FILENAME}.lock"
_LOCK_GUARD_FILENAME = f"{_LOCK_FILENAME}.guard"
_LOCK_POLL_SEC = 0.05
_LOCK_QUEUE_POLL_SEC = 0.005
_LOCK_TIMEOUT_SEC = 5.0
_REPLACE_RETRY_COUNT = 15
_REPLACE_RETRY_DELAY_SEC = 0.02
_READ_RETRY_COUNT = 5
_READ_RETRY_DELAY_SEC = 0.02
_LOCK_OWNER_HOST = socket.gethostname().lower()
NAMESPACE_OPERATION_KEY = "_namespace_operation"
_GET_FINAL_PATH = getattr(os.path, "_getfinalpathname", None)
MAX_TASK_INFO_BYTES = 16 * 1024 * 1024
MAX_SCRIPT_INFO_BYTES = 1024 * 1024
MAX_RUN_HISTORY_SLOTS = 1_000
_RUN_HISTORY_KEYS = (
    "start_times",
    "finish_times",
    "pids",
    "pid_create_times",
    "run_statuses",
    "durations",
    "exit_codes",
    "source_states",
    "run_environments",
    "records",
    "tracks",
)


def namespace_operation_is_live(operation: Any, *, local_host: str = _LOCK_OWNER_HOST) -> bool:
    """Check a move owner only in its PID domain; otherwise honor its lease."""
    if not isinstance(operation, dict):
        return False
    try:
        lease_live = float(operation.get("expires_at", 0.0) or 0.0) > time.time()
    except (TypeError, ValueError, OverflowError):
        lease_live = False
    host = str(operation.get("host", "") or "").lower()
    if host != local_host or operation.get("lock_protocol") != _LOCK_PROTOCOL:
        # Legacy markers and Windows/WSL peers cannot safely use local PID
        # probes, even if they report the same hostname.
        return lease_live

    from pyruns.utils.process_utils import (
        _PROCESS_CREATE_TIME_TOLERANCE_SEC, get_process_create_time, is_pid_running,
    )

    try:
        pid = int(operation.get("pid", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if pid <= 0:
        return False
    expected_create_time = operation.get("pid_create_time")
    actual_create_time = get_process_create_time(pid)
    if expected_create_time is not None and actual_create_time is not None:
        try:
            matches = abs(actual_create_time - float(expected_create_time)) <= _PROCESS_CREATE_TIME_TOLERANCE_SEC
        except (TypeError, ValueError, OverflowError):
            matches = False
        if not matches:
            return False
    # A slow move must remain protected while its known local owner is alive.
    # An unavailable create_time does not prove PID reuse. Failed liveness
    # probes can also mean denied access, so retain the lease in that case.
    return is_pid_running(pid) or lease_live


def guard_task_metric_write(info: Dict[str, Any]) -> None:
    """Reject SDK writes under the task lock before opening a metric store."""
    operation = info.get(NAMESPACE_OPERATION_KEY)
    if isinstance(operation, dict) and namespace_operation_is_live(operation):
        kind = str(operation.get("kind", "move") or "move")
        raise RuntimeError(f"task directory is being prepared for {kind}")


def _thread_lock_for(task_dir: str) -> threading.RLock:
    key = _workspace_abspath(task_dir)
    with _TASK_FILE_LOCKS_GUARD:
        lock = _TASK_FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _TASK_FILE_LOCKS[key] = lock
        return lock


def _replace_with_retry(src: str, dst: str) -> None:
    for attempt in range(_REPLACE_RETRY_COUNT):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt >= _REPLACE_RETRY_COUNT - 1:
                raise
            time.sleep(_REPLACE_RETRY_DELAY_SEC * (attempt + 1))


def _lock_file_snapshot(lock_path: str) -> tuple[tuple[int, int, int, int], bytes] | None:
    try:
        with open(lock_path, "rb") as handle:
            stat = os.fstat(handle.fileno())
            content = handle.read(4096)
    except OSError:
        return None
    identity = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
    return identity, content


def _remove_stale_lock_file(lock_path: str, *, native_guard: bool = False) -> bool:
    snapshot = _lock_file_snapshot(lock_path)
    if snapshot is None or not native_guard:
        return False
    owner_parts = snapshot[1].split()
    if owner_parts[-1:] == [b"released"]:
        owner_parts.pop()
    if len(owner_parts) != 5 or owner_parts[-1] != _LOCK_PROTOCOL.encode("ascii"):
        # Windows and WSL native locks do not interoperate on DrvFS. An old
        # marker or another lock domain cannot be proven orphaned by this
        # guard, even if its numeric PID happens not to exist on this host.
        return False
    if _lock_file_snapshot(lock_path) != snapshot:
        return False

    quarantine_path = f"{lock_path}.stale-{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
    try:
        os.replace(lock_path, quarantine_path)
    except FileNotFoundError:
        return True
    except OSError:
        return False

    if _lock_file_snapshot(quarantine_path) != snapshot:
        try:
            if not os.path.exists(lock_path):
                os.replace(quarantine_path, lock_path)
        except OSError:
            pass
        return False

    try:
        os.remove(quarantine_path)
    except FileNotFoundError:
        pass
    except OSError:
        try:
            if not os.path.exists(lock_path):
                os.replace(quarantine_path, lock_path)
        except OSError:
            pass
        return False
    return True


def _workspace_abspath(path: str) -> str:
    """Preserve literal names in explicitly extended Windows paths."""
    if os.name == "nt":
        value = os.fspath(path)
        if value[:4].replace("/", "\\") == "\\\\?\\":
            return os.path.normpath(value)
    return os.path.abspath(path)


if os.name != "nt":
    _workspace_abspath: Callable[[str], str] = os.path.abspath


def _realpath_anchor(resolved: str) -> str:
    """Remove a Windows prefix only if the plain name still denotes this anchor."""
    if _GET_FINAL_PATH is None or not resolved.startswith("\\\\?\\"):
        return resolved
    plain = "\\\\" + resolved[8:] if resolved[:8].upper() == "\\\\?\\UNC\\" else resolved[4:]
    try:
        if os.path.normcase(_GET_FINAL_PATH(plain)) == os.path.normcase(resolved):
            return plain
    except (OSError, ValueError):
        pass
    return resolved


def _path_is_within(path: str, root: str, *, _resolved_paths: dict[str, str | None] | None = None) -> bool:
    try:
        absolute = _workspace_abspath(path)
        absolute_root = _workspace_abspath(root)
        anchor = _resolved_paths.get(absolute_root) if _resolved_paths is not None else None
        native_paths = False
        if _GET_FINAL_PATH is not None and (anchor is None or anchor.startswith("\\\\?\\")):
            try:
                # Compare existing Windows paths in their native form. realpath
                # otherwise opens each again just to remove the extended prefix.
                # Frozen native anchors can be compared without reopening them.
                resolved_path = _GET_FINAL_PATH(absolute)
                resolved_root = anchor if anchor is not None else _GET_FINAL_PATH(absolute_root)
                native_paths = True
            except (OSError, ValueError):
                # Missing paths need realpath's non-strict naming rules.
                pass
        if not native_paths:
            # Candidates are always fresh. Preserve a frozen anchor even when
            # its ordinary DOS spelling now resolves to a different location.
            resolved_path = os.path.realpath(absolute)
            if anchor is None:
                resolved_root = os.path.realpath(absolute_root)
            elif _GET_FINAL_PATH is None or absolute_root.startswith("\\\\?\\"):
                resolved_root = anchor
            else:
                resolved_root = _realpath_anchor(anchor)
        common = os.path.commonpath([resolved_path, resolved_root])
    except (OSError, ValueError):
        return False
    if os.path.normcase(common) != os.path.normcase(resolved_root):
        return False
    if _resolved_paths is not None:
        for key, resolved in ((absolute_root, resolved_root), (absolute, resolved_path)):
            if key in _resolved_paths and _resolved_paths[key] is None:
                _resolved_paths[key] = resolved
    return True


def _path_is_link_or_reparse(path: str) -> bool:
    """Return whether *path* itself is a symlink, junction, or reparse point."""

    try:
        info = os.lstat(path)
    except OSError:
        return False
    return _stat_is_link_or_reparse(info)


def _stat_is_link_or_reparse(info: os.stat_result | None) -> bool:
    if info is None:
        return False
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def validate_workspace_file(
    path: str,
    workspace_dir: str,
    *,
    label: str,
    _resolved_paths: dict[str, str | None] | None = None,
) -> None:
    """Reject a workspace file that aliases another path or is not a file."""

    absolute = _workspace_abspath(path)
    root = _workspace_abspath(workspace_dir)
    validate_workspace_directory(root)
    try:
        info = os.lstat(absolute)
    except (OSError, ValueError):
        info = None
    if _stat_is_link_or_reparse(info):
        raise ValueError(f"{label} must not be a symlink, junction, or reparse point: {path}")
    if not _path_is_within(absolute, root, _resolved_paths=_resolved_paths):
        raise ValueError(f"{label} resolves outside its workspace boundary: {path}")
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} must be a regular file: {path}")


def validate_workspace_directory(workspace_dir: str) -> None:
    """Reject a managed workspace directory redirected through link metadata."""

    absolute = _workspace_abspath(workspace_dir)
    info = _validate_managed_ancestor_chain(absolute)
    if info is None:
        try:
            info = os.lstat(absolute)
        except (OSError, ValueError):
            return
    if _stat_is_link_or_reparse(info):
        raise ValueError(
            "Workspace directory must not be a symlink, junction, "
            f"or reparse point: {workspace_dir}"
        )
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Workspace path must be a directory: {workspace_dir}")


def validate_tasks_root(tasks_dir: str) -> None:
    """Reject a tasks root that can redirect task I/O through a link/reparse point."""

    absolute = _workspace_abspath(tasks_dir)
    try:
        info = os.lstat(absolute)
    except (OSError, ValueError):
        info = None
    if _stat_is_link_or_reparse(info):
        raise ValueError(
            f"Tasks directory must not be a symlink, junction, or reparse point: {tasks_dir}"
        )
    validate_workspace_directory(os.path.dirname(absolute))


@lru_cache(maxsize=1024)
def _managed_ancestor_paths(absolute: str) -> tuple[str, ...]:
    """Cache only lexical paths; filesystem state must always be checked fresh."""

    ancestors = []
    current = absolute
    while True:
        ancestors.append(current)
        if os.path.normcase(os.path.basename(current)) == os.path.normcase(DEFAULT_ROOT_NAME):
            return tuple(reversed(ancestors))
        parent = os.path.dirname(current)
        if parent == current:
            return ()
        current = parent


def _validate_managed_ancestor_chain(absolute: str) -> os.stat_result | None:
    """Recheck an absolute normalized path and return its fresh leaf metadata."""

    info = None
    for current in _managed_ancestor_paths(absolute):
        try:
            info = os.lstat(current)
        except OSError:
            info = None
        if _stat_is_link_or_reparse(info):
            raise ValueError(
                "Managed workspace path must not contain a symlink, junction, "
                f"or reparse point: {current}"
            )
    return info


def validate_task_directory(task_dir: str, *, _resolved_paths: dict[str, str | None] | None = None) -> None:
    """Reject task paths that can alias another directory through reparse metadata."""

    absolute = _workspace_abspath(task_dir)
    validate_tasks_root(os.path.dirname(absolute))
    exists = os.path.lexists(absolute)
    if exists and not _path_is_within(absolute, os.path.dirname(absolute), _resolved_paths=_resolved_paths):
        raise ValueError(f"Task directory resolves outside the tasks directory: {task_dir}")
    if exists and _path_is_link_or_reparse(absolute):
        raise ValueError(
            f"Task directory must not be a symlink, junction, or reparse point: {task_dir}"
        )


def _task_log_directory(task_dir: str, *, create: bool, _resolved_paths: dict[str, str | None] | None = None) -> str:
    validate_task_directory(task_dir, _resolved_paths=_resolved_paths)
    absolute_task = _workspace_abspath(task_dir)
    log_dir = os.path.join(absolute_task, RUN_LOGS_DIR)
    if os.path.lexists(log_dir):
        if _path_is_link_or_reparse(log_dir):
            raise ValueError(
                "Run logs directory must not be a symlink, junction, "
                f"or reparse point: {log_dir}"
            )
        if not os.path.isdir(log_dir):
            raise ValueError(f"Run logs path must be a directory: {log_dir}")
    elif create:
        os.makedirs(log_dir, exist_ok=True)

    if os.path.lexists(log_dir) and (
        _path_is_link_or_reparse(log_dir)
        or not _path_is_within(log_dir, absolute_task, _resolved_paths=_resolved_paths)
    ):
        raise ValueError(f"Run logs directory resolves outside the task boundary: {log_dir}")
    return log_dir


def _task_log_path(task_dir: str, filename: str, *, create_directory: bool) -> str:
    """Resolve one task log path without following managed links."""

    if not filename or os.path.basename(filename) != filename or filename in {os.curdir, os.pardir}:
        raise ValueError(f"Log filename must be one path component: {filename}")

    log_dir = _task_log_directory(task_dir, create=create_directory)

    log_path = os.path.join(log_dir, filename)
    exists = os.path.lexists(log_path)
    if exists and _path_is_link_or_reparse(log_path):
        raise ValueError(
            "Log file must not be a symlink, junction, "
            f"or reparse point: {log_path}"
        )
    if not _path_is_within(log_path, log_dir):
        raise ValueError(f"Log path resolves outside the run logs directory: {log_path}")
    if exists:
        if not os.path.isfile(log_path):
            raise ValueError(f"Log path must be a regular file: {log_path}")
    return log_path


def validate_task_log_path(task_dir: str, filename: str) -> str:
    """Return a safe task log path without creating its directory."""

    return _task_log_path(task_dir, filename, create_directory=False)


def prepare_task_log_path(task_dir: str, filename: str) -> str:
    """Return a safe writable task log path, creating its directory when needed."""

    return _task_log_path(task_dir, filename, create_directory=True)


def _validate_contained_path(
    path: str,
    root: str,
    *,
    label: str,
    _resolved_paths: dict[str, str | None] | None = None,
) -> None:
    if os.path.lexists(path) and not _path_is_within(path, root, _resolved_paths=_resolved_paths):
        raise ValueError(f"{label} resolves outside its workspace boundary: {path}")


def _load_json_object(
    path: str, *, max_bytes: int, label: str, _boundary: str | None = None,
) -> Dict[str, Any]:
    with open(path, "rb", opener=regular_file_opener) as handle:
        if _boundary is not None:
            validate_open_file(handle, path, _boundary)
        raw = read_bounded_bytes(handle, max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"{label} is too large (max {max_bytes} bytes): {path}")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{label} root must be a JSON object: {path}")
    return data


def _mark_task_lock_released(fd: int, owner: str) -> str:
    """Let waiters recover a completed lock if Windows delays its deletion."""
    owner_bytes = owner.encode("utf-8", errors="ignore")
    marker = b" released"
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.read(fd, len(owner_bytes) + 1) == owner_bytes:
            written = os.write(fd, marker)
            return (owner + marker[:written].decode("ascii")).rstrip()
    except OSError:
        pass
    return owner


def _release_task_lock(lock_path: str, owner: str, *, identity: tuple[int, int] | None = None) -> None:
    """Retry temporary sharing violations without deleting another owner's lock."""
    for attempt in range(_REPLACE_RETRY_COUNT):
        try:
            if identity is not None:
                current = os.lstat(lock_path)
                if (current.st_dev, current.st_ino) != identity:
                    return
            else:
                with open(lock_path, "r", encoding="utf-8") as handle:
                    current_owner = handle.read().strip()
                if current_owner != owner:
                    return
            os.remove(lock_path)
            return
        except PermissionError:
            # Windows readers can temporarily deny deletion. Leaving this file
            # behind would block the still-live owner on its next update.
            if attempt < _REPLACE_RETRY_COUNT - 1:
                time.sleep(_REPLACE_RETRY_DELAY_SEC * (attempt + 1))
        except OSError:
            break

    # A live owner must remain non-reclaimable until its last unlink attempt.
    # Publishing "released" before deletion lets a waiter replace this file
    # while the old owner is between its ownership check and os.remove().
    # If Windows keeps denying deletion, publish the marker as the last action
    # and leave all subsequent removal to the next writer.
    if identity is None:
        try:
            with open(lock_path, "r+b", buffering=0) as handle:
                _mark_task_lock_released(handle.fileno(), owner)
        except OSError:
            pass


@contextmanager
def task_info_lock(task_dir: str, timeout_sec: float = _LOCK_TIMEOUT_SEC, *, create_dir: bool = True):
    """Acquire a task-local thread/process lock for task_info.json updates."""
    from pyruns.utils.lock_queue import TaskLockQueue

    validate_task_directory(task_dir)
    thread_lock = _thread_lock_for(task_dir)
    lock_path = os.path.join(task_dir, _LOCK_FILENAME)
    if create_dir:
        os.makedirs(task_dir, exist_ok=True)
    elif not os.path.isdir(task_dir):
        raise FileNotFoundError(task_dir)
    acquired = thread_lock.acquire(timeout=timeout_sec)
    if not acquired:
        raise TimeoutError(f"Timed out acquiring task lock for {task_dir}")

    fd: Optional[int] = None
    guard = NativeFileGuard(os.path.join(task_dir, _LOCK_GUARD_FILENAME), task_dir, label="Task lock guard")
    owner = f"{os.getpid()} {threading.get_ident()} {_LOCK_OWNER_HOST} {time.time():.6f} {_LOCK_PROTOCOL}"
    owner_bytes = owner.encode("utf-8", errors="ignore")
    owner_written = False
    start = time.monotonic()
    permission_failures = 0
    queue = TaskLockQueue(task_dir)
    try:
        while True:
            remaining = timeout_sec - (time.monotonic() - start)
            if not guard.acquired and queue.entry is None and queue.has_waiters():
                if remaining <= 0:
                    raise TimeoutError(f"Timed out acquiring file lock for {task_dir}")
                queue.register(remaining)
            if not guard.acquired and not queue.is_first():
                if remaining <= 0:
                    raise TimeoutError(f"Timed out acquiring file lock for {task_dir}")
                time.sleep(min(_LOCK_QUEUE_POLL_SEC, remaining))
                continue
            if not guard.acquired:
                if not guard.try_acquire():
                    if remaining <= 0:
                        raise TimeoutError(f"Timed out acquiring file lock for {task_dir}")
                    if queue.entry is None:
                        queue.register(remaining)
                    time.sleep(min(_LOCK_QUEUE_POLL_SEC, remaining))
                    continue
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except FileExistsError as exc:
                if _remove_stale_lock_file(lock_path, native_guard=True):
                    continue
                if time.monotonic() - start >= timeout_sec:
                    raise TimeoutError(
                        f"Timed out acquiring file lock for {task_dir}. "
                        "An older or different runtime may own this lock. Let it finish; "
                        "for an abandoned lock, recover from its original runtime or remove "
                        "only the .task_info.json.lock file after confirming all writers have stopped."
                    ) from exc
                time.sleep(min(_LOCK_QUEUE_POLL_SEC, max(0.0, timeout_sec - (time.monotonic() - start))))
            except PermissionError:
                # Windows can deny exclusive creation while a competing lock
                # is being deleted. Keep permanent permission errors intact.
                permission_failures += 1
                remaining = timeout_sec - (time.monotonic() - start)
                if permission_failures >= _REPLACE_RETRY_COUNT or remaining <= 0:
                    raise
                time.sleep(min(_LOCK_POLL_SEC, remaining))
            else:
                if os.write(fd, owner_bytes) != len(owner_bytes):
                    raise OSError("Could not write the complete task lock owner")
                owner_written = True
                break
        yield
    finally:
        try:
            if fd is not None:
                failed_identity = None
                try:
                    if not owner_written:
                        # A partial owner cannot identify the lock. Capture its
                        # file identity before close allows a replacement owner.
                        try:
                            info = os.fstat(fd)
                            failed_identity = info.st_dev, info.st_ino
                        except OSError:
                            pass
                finally:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    if owner_written:
                        _release_task_lock(lock_path, owner)
                    elif failed_identity is not None:
                        _release_task_lock(lock_path, owner, identity=failed_identity)
        finally:
            try:
                guard.close()
            finally:
                try:
                    queue.close()
                finally:
                    thread_lock.release()


def load_task_metadata(
    task_dir: str,
    raise_error: bool = False,
    *,
    _resolved_paths: dict[str, str | None] | None = None,
) -> Dict[str, Any]:
    """Load task control data without materializing externally stored curves."""
    info_path = os.path.join(task_dir, TASK_INFO_FILENAME)
    try:
        resolved_paths: dict[str, str | None] = {} if _resolved_paths is None else _resolved_paths
        resolved_paths.setdefault(_workspace_abspath(task_dir), None)
        validate_task_directory(task_dir, _resolved_paths=resolved_paths)
        if not os.path.exists(info_path):
            if raise_error:
                raise FileNotFoundError(info_path)
            return {}
        for attempt in range(_READ_RETRY_COUNT):
            try:
                _validate_contained_path(
                    info_path, task_dir, label=TASK_INFO_FILENAME, _resolved_paths=resolved_paths,
                )
                info = _load_json_object(
                    info_path,
                    max_bytes=MAX_TASK_INFO_BYTES,
                    label=TASK_INFO_FILENAME,
                    _boundary=resolved_paths[_workspace_abspath(task_dir)],
                )
                if info.get("env") is not None and not isinstance(info["env"], dict):
                    raise ValueError("Invalid task environment: expected an object")
                info.pop("id", None)
                normalize_run_history(info)
                return info
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                if attempt >= _READ_RETRY_COUNT - 1:
                    raise
                time.sleep(_READ_RETRY_DELAY_SEC * (attempt + 1))
    except Exception:
        if raise_error:
            raise
        return {}
    return {}


def load_task_info(task_dir: str, raise_error: bool = False) -> Dict[str, Any]:
    """Load complete task info, including legacy and external track histories."""
    from pyruns.utils.track_store import TRACK_STORE_KEY, MissingTrackGeneration, read_tracks

    info = load_task_metadata(task_dir, raise_error=raise_error)
    if TRACK_STORE_KEY not in info:
        return info
    try:
        attempt = 0
        while True:
            try:
                if TRACK_STORE_KEY in info:
                    info["tracks"] = read_tracks(task_dir, info[TRACK_STORE_KEY], slots=run_slot_count(info))
                return info
            except MissingTrackGeneration:
                # Replacement commits the new generation before switching JSON.
                # A pruned pointer is retried only when metadata actually changed.
                attempt += 1
                latest = load_task_metadata(task_dir, raise_error=True)
                if attempt >= _READ_RETRY_COUNT or latest.get(TRACK_STORE_KEY) == info.get(TRACK_STORE_KEY):
                    raise
                info = latest
    except Exception:
        if raise_error:
            raise
        return {}


def _save_full_task_info_unlocked(info_path: str, task_dir: str, payload: Dict[str, Any]) -> None:
    """Commit a prepared curve generation before switching the atomic pointer."""
    from pyruns.utils.track_store import (
        TRACK_STORE_KEY, new_descriptor, prepare_generation, prune_generations, should_externalize,
    )

    if TRACK_STORE_KEY not in payload and not should_externalize(payload.get("tracks", [])):
        _write_task_info_unlocked(info_path, task_dir, payload)
        return
    descriptor = new_descriptor(payload["tracks"])
    stored = {**payload, "tracks": [{} for _ in payload["tracks"]], TRACK_STORE_KEY: descriptor}
    _serialize_task_info(info_path, stored)
    prepare_generation(task_dir, payload["tracks"], descriptor)
    _write_task_info_unlocked(info_path, task_dir, stored)
    payload[TRACK_STORE_KEY] = descriptor
    try:
        prune_generations(task_dir, descriptor)
    except Exception as exc:
        # Cleanup is not part of the commit. Reporting a committed update as
        # failed could make an SDK retry it and duplicate a metric point.
        logging.getLogger(__name__).debug("Could not prune old track generations: %s", exc)


def save_task_info(task_dir: str, info: Dict[str, Any]) -> None:
    """Save task_info.json atomically after normalizing run-slot fields."""
    validate_task_directory(task_dir)
    os.makedirs(task_dir, exist_ok=True)
    info_path = os.path.join(task_dir, TASK_INFO_FILENAME)
    payload = copy.deepcopy(info)
    payload.pop("id", None)
    normalize_run_history(payload)
    with task_info_lock(task_dir):
        _save_full_task_info_unlocked(info_path, task_dir, payload)


def load_script_info(run_root: str) -> Dict[str, Any]:
    """Load script_info.json from the run root directory."""
    script_info_path = os.path.join(run_root, SCRIPT_INFO_FILENAME)
    try:
        validate_workspace_file(
            script_info_path,
            run_root,
            label=SCRIPT_INFO_FILENAME,
        )
        if not os.path.exists(script_info_path):
            return {}
        return _load_json_object(
            script_info_path,
            max_bytes=MAX_SCRIPT_INFO_BYTES,
            label=SCRIPT_INFO_FILENAME,
        )
    except Exception:
        return {}


def save_script_info(run_root: str, info: Dict[str, Any]) -> None:
    """Save script_info.json atomically to the run root directory."""
    validate_workspace_directory(run_root)
    os.makedirs(run_root, exist_ok=True)
    validate_workspace_directory(run_root)
    script_info_path = os.path.join(run_root, SCRIPT_INFO_FILENAME)
    validate_workspace_file(
        script_info_path,
        run_root,
        label=SCRIPT_INFO_FILENAME,
    )
    serialized = json.dumps(info, indent=2, ensure_ascii=False, allow_nan=False)
    if len(serialized.encode("utf-8")) > MAX_SCRIPT_INFO_BYTES:
        raise ValueError(
            f"{SCRIPT_INFO_FILENAME} is too large (max {MAX_SCRIPT_INFO_BYTES} bytes): {script_info_path}"
        )
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{SCRIPT_INFO_FILENAME}.",
        suffix=".tmp",
        dir=run_root,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        validate_workspace_file(
            script_info_path,
            run_root,
            label=SCRIPT_INFO_FILENAME,
        )
        _replace_with_retry(tmp_path, script_info_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def extract_metrics(info: Dict[str, Any]) -> list:
    """Safely extract record rows from task info."""
    return info.get("records", [])


def load_record_data(task_dir: str) -> list:
    """Load record entries from task_info.json."""
    info = load_task_metadata(task_dir)
    return extract_metrics(info)


def update_task_info(
    task_dir: str,
    updater: Callable[[Dict[str, Any]], None],
    *,
    timeout_sec: float = _LOCK_TIMEOUT_SEC,
    include_tracks: bool = True,
) -> Dict[str, Any]:
    """Strictly update an existing task_info.json through the atomic save path."""
    validate_task_directory(task_dir)
    info_path = os.path.join(task_dir, TASK_INFO_FILENAME)
    with task_info_lock(task_dir, timeout_sec=timeout_sec, create_dir=False):
        _validate_contained_path(info_path, task_dir, label=TASK_INFO_FILENAME)
        info = _load_json_object(
            info_path,
            max_bytes=MAX_TASK_INFO_BYTES,
            label=TASK_INFO_FILENAME,
        )

        info.pop("id", None)
        normalize_run_history(info)
        from pyruns.utils.track_store import TRACK_STORE_KEY, read_tracks

        if include_tracks and TRACK_STORE_KEY in info:
            info["tracks"] = read_tracks(task_dir, info[TRACK_STORE_KEY], slots=run_slot_count(info))
        updater(info)
        payload = copy.deepcopy(info)
        payload.pop("id", None)
        normalize_run_history(payload)
        if include_tracks:
            _save_full_task_info_unlocked(info_path, task_dir, payload)
        else:
            if TRACK_STORE_KEY in payload and any(payload["tracks"]):
                raise ValueError("Use a full task update when replacing externally stored tracks")
            if TRACK_STORE_KEY in payload:
                _write_task_info_unlocked(info_path, task_dir, payload)
            else:
                _save_full_task_info_unlocked(info_path, task_dir, payload)
                if TRACK_STORE_KEY in payload:
                    payload["tracks"] = [{} for _ in payload["tracks"]]
        return payload


def update_task_metadata(
    task_dir: str,
    updater: Callable[[Dict[str, Any]], None],
    *,
    timeout_sec: float = _LOCK_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """Update control fields while preserving an external curve generation."""
    return update_task_info(task_dir, updater, timeout_sec=timeout_sec, include_tracks=False)


def append_task_track(task_dir: str, data: Dict[str, Any], *, run_index: int | None = None) -> None:
    """Persist one SDK track update without rewriting a large history."""
    from pyruns.utils.track_store import encode_values

    append_task_track_payloads(task_dir, (encode_values(data),), run_index=run_index)


def append_task_track_payloads(task_dir: str, payloads: tuple[str, ...], *, run_index: int | None = None) -> None:
    """Persist already validated JSON points together under the task write lock."""
    from pyruns.utils.track_store import TRACK_STORE_KEY, append_point, append_points

    if not payloads:
        return
    with task_info_lock(task_dir, create_dir=False):
        info = load_task_metadata(task_dir, raise_error=True)
        guard_task_metric_write(info)
        previous_slots = run_slot_count(info)
        target = run_index if run_index is not None else max(1, previous_slots)
        slot = ensure_run_slot(info, target)
        target = slot + 1
        info_path = os.path.join(task_dir, TASK_INFO_FILENAME)
        if TRACK_STORE_KEY in info:
            descriptor = info[TRACK_STORE_KEY]
            if target > previous_slots or target > int(descriptor.get("max_run_index", 0)):
                descriptor["max_run_index"] = max(target, int(descriptor.get("max_run_index", 0)))
                _write_task_info_unlocked(info_path, task_dir, info)
            if len(payloads) == 1:
                append_point(task_dir, descriptor, target, payloads[0])
            else:
                append_points(task_dir, descriptor, target, payloads)
            return
        for payload in payloads:
            for key, value in json.loads(payload).items():
                info["tracks"][slot].setdefault(key, []).append(value)
        _save_full_task_info_unlocked(info_path, task_dir, info)


def run_slot_count(meta: Dict[str, Any]) -> int:
    """Return the aligned run-slot count for *meta*."""
    lengths = []
    for key in _RUN_HISTORY_KEYS:
        values = meta.get(key, []) or []
        if not isinstance(values, (list, tuple)):
            raise ValueError(f"Invalid run history field '{key}': expected an array")
        lengths.append(len(values))
    try:
        run_index = int(meta.get("run_index", meta.get("_run_index", 0)) or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid run history index") from exc
    if run_index < 0:
        raise ValueError("Invalid run history index: must not be negative")
    total = max([run_index, *lengths], default=0)
    if total > MAX_RUN_HISTORY_SLOTS:
        raise ValueError(
            f"Invalid run history: {total} slots exceeds the limit of {MAX_RUN_HISTORY_SLOTS}"
        )
    return total


def ensure_run_slot(meta: Dict[str, Any], run_index: int) -> int:
    """Pad run arrays so that *run_index* exists and return the zero-based slot."""
    target = max(int(run_index or 0), 1)
    if target > MAX_RUN_HISTORY_SLOTS:
        raise ValueError(
            f"Invalid run history: {target} slots exceeds the limit of {MAX_RUN_HISTORY_SLOTS}"
        )
    meta["start_times"] = list(meta.get("start_times", []) or [])
    meta["finish_times"] = list(meta.get("finish_times", []) or [])
    meta["pids"] = list(meta.get("pids", []) or [])
    meta["pid_create_times"] = list(meta.get("pid_create_times", []) or [])
    meta["run_statuses"] = list(meta.get("run_statuses", []) or [])
    meta["durations"] = list(meta.get("durations", []) or [])
    meta["exit_codes"] = list(meta.get("exit_codes", []) or [])
    meta["source_states"] = list(meta.get("source_states", []) or [])
    meta["run_environments"] = list(meta.get("run_environments", []) or [])
    meta["records"] = list(meta.get("records", []) or [])
    meta["tracks"] = list(meta.get("tracks", []) or [])

    while len(meta["start_times"]) < target:
        meta["start_times"].append("")
    while len(meta["finish_times"]) < target:
        meta["finish_times"].append("")
    while len(meta["pids"]) < target:
        meta["pids"].append(None)
    while len(meta["pid_create_times"]) < target:
        meta["pid_create_times"].append(None)
    while len(meta["run_statuses"]) < target:
        meta["run_statuses"].append("")
    while len(meta["durations"]) < target:
        meta["durations"].append(None)
    while len(meta["exit_codes"]) < target:
        meta["exit_codes"].append(None)
    while len(meta["source_states"]) < target:
        meta["source_states"].append("")
    while len(meta["run_environments"]) < target:
        meta["run_environments"].append(None)
    while len(meta["records"]) < target:
        meta["records"].append({})
    while len(meta["tracks"]) < target:
        meta["tracks"].append({})

    meta["run_index"] = max(int(meta.get("run_index", 0) or 0), target)
    meta.pop("_run_index", None)
    return target - 1


def get_log_entries(task_dir: str, *, cancelled: threading.Event | None = None) -> dict[str, tuple[str, os.stat_result]]:
    """Enumerate safe direct log files, reusing scandir metadata for search."""
    if cancelled is not None and cancelled.is_set():
        raise CancelledError()
    opts: dict[str, tuple[str, os.stat_result]] = {}
    absolute_task = _workspace_abspath(task_dir)
    # Retain only these three directory anchors, for this enumeration alone.
    # A redirected parent must not change the boundary of later file checks.
    resolved_paths: dict[str, str | None] = dict.fromkeys((
        os.path.dirname(absolute_task), absolute_task, os.path.join(absolute_task, RUN_LOGS_DIR),
    ))
    try:
        run_dir = _task_log_directory(task_dir, create=False, _resolved_paths=resolved_paths)
    except ValueError:
        return opts
    if os.path.isdir(run_dir):
        with os.scandir(run_dir) as entries:
            for entry in entries:
                if cancelled is not None and cancelled.is_set():
                    raise CancelledError()
                name = entry.name
                if name not in {QUEUE_LOG_FILENAME, ERROR_LOG_FILENAME} and not (
                    name.startswith("run") and name.endswith(".log")
                ):
                    continue
                try:
                    # Windows DirEntry.stat omits device/inode values needed
                    # for cache invalidation and the log-context identity.
                    info = os.lstat(entry.path) if os.name == "nt" else entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                # Direct directory entries cannot escape this anchor unless
                # they are links/reparse points. Avoid a realpath per file.
                if stat.S_ISREG(info.st_mode) and not _stat_is_link_or_reparse(info):
                    opts[name] = (entry.path, info)

        # Discard earlier entries too if their parent was redirected while
        # later files were being enumerated.
        try:
            if opts:
                anchor = resolved_paths[run_dir]
                current = (
                    _GET_FINAL_PATH(run_dir)
                    if _GET_FINAL_PATH is not None and anchor is not None and anchor.startswith("\\\\?\\")
                    else os.path.realpath(run_dir)
                )
                if current != anchor:
                    return {}
        except (OSError, ValueError):
            return {}

    def order(name):
        if name == QUEUE_LOG_FILENAME:
            return (0, 0, name)
        if name == ERROR_LOG_FILENAME:
            return (2, 0, name)
        return (1, int("".join(filter(str.isdecimal, name)) or "0"), name)

    return {name: opts[name] for name in sorted(opts, key=order)}


def get_log_options(task_dir: str) -> Dict[str, str]:
    """Return ``{display_name: file_path}`` for all available log files."""
    return {name: path for name, (path, _) in get_log_entries(task_dir).items()}


def resolve_log_path(task_dir: str, log_file_name: Optional[str] = None) -> Optional[str]:
    """Resolve which log file to display for a task."""
    opts = get_log_options(task_dir)
    if log_file_name:
        return opts.get(log_file_name)
    if opts:
        info = load_task_metadata(task_dir) or {}
        status = str(info.get("status", "") or "").lower()
        if status == "queued" and QUEUE_LOG_FILENAME in opts:
            return opts[QUEUE_LOG_FILENAME]

        latest_run = run_slot_count(info)
        expected_name = f"run{latest_run}.log" if latest_run > 0 else ""
        if expected_name and expected_name in opts:
            return opts[expected_name]
        if status == "failed" and ERROR_LOG_FILENAME in opts:
            return opts[ERROR_LOG_FILENAME]

        run_logs = {
            name: path
            for name, path in opts.items()
            if name.startswith("run") and name.endswith(".log")
        }
        candidates = run_logs or {
            name: path for name, path in opts.items() if name != QUEUE_LOG_FILENAME
        } or opts
        cached = [(f, p, os.path.getmtime(p)) for f, p in candidates.items()]
        cached.sort(key=lambda x: x[2], reverse=True)
        return cached[0][1]
    return None


_INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAME_RE = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$",
    re.IGNORECASE,
)


def _validate_task_folder_name(name: str) -> Optional[str]:
    if not name or not name.strip():
        return "Task name cannot be empty"
    raw_name = name
    if raw_name != raw_name.strip():
        return "Task name cannot start or end with whitespace"
    name = raw_name
    if len(name) > 200:
        return "Task name is too long (max 200 characters)"
    if "@" in name:
        return "Task name cannot contain '@'; it is reserved for TASK@RUN references"
    bad = _INVALID_CHARS_RE.findall(name)
    if bad:
        return f"Task name contains invalid characters: {''.join(set(bad))}"
    if name.startswith("."):
        return "Task name cannot start with '.'"
    if raw_name.endswith("."):
        return "Task name cannot end with '.'"
    if name.startswith("-"):
        return "Task name cannot start with '-'; it can be confused with an option"
    if _WINDOWS_RESERVED_NAME_RE.fullmatch(name):
        return f"Task name '{name}' is reserved on Windows"

    return None


def validate_task_name(name: str, root_dir: Optional[str] = None) -> Optional[str]:
    """Validate whether a new task name can be used as a folder name."""
    error = _validate_task_folder_name(name)
    if error:
        return error
    name = name.strip()

    if root_dir and os.path.exists(os.path.join(root_dir, name)):
        return f"Task name '{name}' already exists in the current workspace"
    return None


def normalize_run_history(meta: Dict[str, Any]) -> int:
    """Align run-slot arrays without discarding failed or incomplete runs."""
    total = run_slot_count(meta)

    starts = list(meta.get("start_times", []) or [])
    finishes = list(meta.get("finish_times", []) or [])
    pids = list(meta.get("pids", []) or [])
    pid_create_times = list(meta.get("pid_create_times", []) or [])
    run_statuses = list(meta.get("run_statuses", []) or [])
    durations = list(meta.get("durations", []) or [])
    exit_codes = list(meta.get("exit_codes", []) or [])
    source_states = list(meta.get("source_states", []) or [])
    run_environments = list(meta.get("run_environments", []) or [])
    records = list(meta.get("records", []) or [])
    tracks = list(meta.get("tracks", []) or [])

    while len(starts) < total:
        starts.append("")
    while len(finishes) < total:
        finishes.append("")
    while len(pids) < total:
        pids.append(None)
    while len(pid_create_times) < total:
        pid_create_times.append(None)
    while len(run_statuses) < total:
        run_statuses.append("")
    while len(durations) < total:
        durations.append(None)
    while len(exit_codes) < total:
        exit_codes.append(None)
    while len(source_states) < total:
        source_states.append("")
    while len(run_environments) < total:
        run_environments.append(None)
    while len(records) < total:
        records.append({})
    while len(tracks) < total:
        tracks.append({})

    meta["start_times"] = starts[:total]
    meta["finish_times"] = finishes[:total]
    meta["pids"] = pids[:total]
    meta["pid_create_times"] = pid_create_times[:total]
    meta["run_statuses"] = run_statuses[:total]
    meta["durations"] = durations[:total]
    meta["exit_codes"] = exit_codes[:total]
    meta["source_states"] = source_states[:total]
    meta["run_environments"] = run_environments[:total]
    meta["records"] = records[:total]
    meta["tracks"] = tracks[:total]
    meta["run_index"] = total
    meta.pop("_run_index", None)
    return total


def _serialize_task_info(info_path: str, payload: Dict[str, Any]) -> str:
    serialized = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
    if len(serialized.encode("utf-8")) > MAX_TASK_INFO_BYTES:
        raise ValueError(
            f"{TASK_INFO_FILENAME} is too large (max {MAX_TASK_INFO_BYTES} bytes): {info_path}"
        )
    return serialized


def _write_task_info_unlocked(info_path: str, task_dir: str, payload: Dict[str, Any]) -> None:
    """Write task info atomically; caller must already hold task_info_lock()."""
    serialized = _serialize_task_info(info_path, payload)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{TASK_INFO_FILENAME}.",
        suffix=".tmp",
        dir=task_dir,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp_path, info_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
