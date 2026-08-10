"""Task-level I/O helpers shared by core, UI, and public APIs."""

from __future__ import annotations

import copy
import json
import os
import re
import socket
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Optional

from pyruns._config import (
    DEFAULT_ROOT_NAME,
    ERROR_LOG_FILENAME,
    QUEUE_LOG_FILENAME,
    RUN_LOGS_DIR,
    SCRIPT_INFO_FILENAME,
    TASK_INFO_FILENAME,
)
from pyruns.utils.process_utils import get_process_create_time, is_pid_running

_TASK_FILE_LOCKS: Dict[str, threading.RLock] = {}
_TASK_FILE_LOCKS_GUARD = threading.Lock()
_LOCK_FILENAME = f".{TASK_INFO_FILENAME}.lock"
_LOCK_POLL_SEC = 0.05
_LOCK_TIMEOUT_SEC = 5.0
_REPLACE_RETRY_COUNT = 5
_REPLACE_RETRY_DELAY_SEC = 0.02
_READ_RETRY_COUNT = 5
_READ_RETRY_DELAY_SEC = 0.02
_STALE_LOCK_MIN_AGE_SEC = 30.0
_LOCK_OWNER_HOST = socket.gethostname().lower()
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
    "records",
    "tracks",
)


def _thread_lock_for(task_dir: str) -> threading.RLock:
    key = os.path.abspath(task_dir)
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


def _read_lock_owner(lock_path: str) -> tuple[Optional[int], str, Optional[float]]:
    try:
        with open(lock_path, "r", encoding="utf-8") as handle:
            parts = handle.read().strip().split()
    except OSError:
        return None, "", None
    pid = None
    if parts:
        try:
            pid = int(parts[0])
        except (TypeError, ValueError):
            pid = None
    host = parts[2].lower() if len(parts) >= 3 else ""
    acquired_at = None
    if len(parts) >= 4:
        try:
            acquired_at = float(parts[3])
        except (TypeError, ValueError, OverflowError):
            acquired_at = None
    return pid, host, acquired_at


def _lock_file_is_stale(lock_path: str, *, min_age_sec: float = _STALE_LOCK_MIN_AGE_SEC) -> bool:
    try:
        age = time.time() - os.path.getmtime(lock_path)
    except OSError:
        return False

    pid, host, acquired_at = _read_lock_owner(lock_path)
    if pid is not None and host and host != _LOCK_OWNER_HOST:
        return False
    if pid is not None:
        if not is_pid_running(pid):
            return True
        if acquired_at is not None:
            process_created_at = get_process_create_time(pid)
            if (
                process_created_at is not None
                and process_created_at > acquired_at + 0.01
            ):
                return True
        return False
    return age >= max(0.0, min_age_sec)


def _lock_file_snapshot(lock_path: str) -> tuple[tuple[int, int, int, int], bytes] | None:
    try:
        with open(lock_path, "rb") as handle:
            stat = os.fstat(handle.fileno())
            content = handle.read(4096)
    except OSError:
        return None
    identity = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
    return identity, content


def _remove_stale_lock_file(lock_path: str) -> bool:
    snapshot = _lock_file_snapshot(lock_path)
    if snapshot is None or not _lock_file_is_stale(lock_path):
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


def _path_is_within(path: str, root: str) -> bool:
    try:
        resolved_path = os.path.realpath(os.path.abspath(path))
        resolved_root = os.path.realpath(os.path.abspath(root))
        common = os.path.commonpath([resolved_path, resolved_root])
    except (OSError, ValueError):
        return False
    return os.path.normcase(common) == os.path.normcase(resolved_root)


def _path_is_link_or_reparse(path: str) -> bool:
    """Return whether *path* itself is a symlink, junction, or reparse point."""

    try:
        if os.path.islink(path):
            return True
        isjunction = getattr(os.path, "isjunction", None)
        if isjunction is not None and isjunction(path):
            return True
        attributes = int(getattr(os.lstat(path), "st_file_attributes", 0) or 0)
    except OSError:
        return False
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def validate_workspace_file(path: str, workspace_dir: str, *, label: str) -> None:
    """Reject a workspace file that aliases another path or is not a file."""

    absolute = os.path.abspath(path)
    root = os.path.abspath(workspace_dir)
    validate_workspace_directory(root)
    exists = os.path.lexists(absolute)
    if exists and _path_is_link_or_reparse(absolute):
        raise ValueError(f"{label} must not be a symlink, junction, or reparse point: {path}")
    if not _path_is_within(absolute, root):
        raise ValueError(f"{label} resolves outside its workspace boundary: {path}")
    if not exists:
        return
    if not os.path.isfile(absolute):
        raise ValueError(f"{label} must be a regular file: {path}")


def validate_workspace_directory(workspace_dir: str) -> None:
    """Reject a managed workspace directory redirected through link metadata."""

    absolute = os.path.abspath(workspace_dir)
    _validate_managed_ancestor_chain(absolute)
    if os.path.lexists(absolute):
        if _path_is_link_or_reparse(absolute):
            raise ValueError(
                "Workspace directory must not be a symlink, junction, "
                f"or reparse point: {workspace_dir}"
            )
        if not os.path.isdir(absolute):
            raise ValueError(f"Workspace path must be a directory: {workspace_dir}")


def validate_tasks_root(tasks_dir: str) -> None:
    """Reject a tasks root that can redirect task I/O through a link/reparse point."""

    absolute = os.path.abspath(tasks_dir)
    if os.path.lexists(absolute) and _path_is_link_or_reparse(absolute):
        raise ValueError(
            f"Tasks directory must not be a symlink, junction, or reparse point: {tasks_dir}"
        )
    validate_workspace_directory(os.path.dirname(absolute))


def _validate_managed_ancestor_chain(path: str) -> None:
    """Reject links from ``_pyruns_`` through an existing managed path."""

    absolute = os.path.abspath(path)
    managed_root: str | None = None
    current = absolute
    while True:
        if os.path.normcase(os.path.basename(current)) == os.path.normcase(DEFAULT_ROOT_NAME):
            managed_root = current
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    if managed_root is None:
        return

    relative = os.path.relpath(absolute, managed_root)
    components = [] if relative == os.curdir else relative.split(os.sep)
    current = managed_root
    for component in ["", *components]:
        if component:
            current = os.path.join(current, component)
        if os.path.lexists(current) and _path_is_link_or_reparse(current):
            raise ValueError(
                "Managed workspace path must not contain a symlink, junction, "
                f"or reparse point: {current}"
            )


def validate_task_directory(task_dir: str) -> None:
    """Reject task paths that can alias another directory through reparse metadata."""

    absolute = os.path.abspath(task_dir)
    validate_tasks_root(os.path.dirname(absolute))
    exists = os.path.lexists(absolute)
    if exists and not _path_is_within(absolute, os.path.dirname(absolute)):
        raise ValueError(f"Task directory resolves outside the tasks directory: {task_dir}")
    if exists and _path_is_link_or_reparse(absolute):
        raise ValueError(
            f"Task directory must not be a symlink, junction, or reparse point: {task_dir}"
        )


def _task_log_directory(task_dir: str, *, create: bool) -> str:
    validate_task_directory(task_dir)
    absolute_task = os.path.abspath(task_dir)
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
        _path_is_link_or_reparse(log_dir) or not _path_is_within(log_dir, absolute_task)
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


def _validate_contained_path(path: str, root: str, *, label: str) -> None:
    if os.path.lexists(path) and not _path_is_within(path, root):
        raise ValueError(f"{label} resolves outside its workspace boundary: {path}")


def _load_json_object(path: str, *, max_bytes: int, label: str) -> Dict[str, Any]:
    with open(path, "rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"{label} is too large (max {max_bytes} bytes): {path}")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{label} root must be a JSON object: {path}")
    return data


@contextmanager
def task_info_lock(task_dir: str, timeout_sec: float = _LOCK_TIMEOUT_SEC, *, create_dir: bool = True):
    """Acquire a task-local thread/process lock for task_info.json updates."""
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
    owner = f"{os.getpid()} {threading.get_ident()} {_LOCK_OWNER_HOST} {time.time():.6f}"
    start = time.monotonic()
    try:
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(fd, owner.encode("utf-8", errors="ignore"))
                break
            except FileExistsError:
                if _remove_stale_lock_file(lock_path):
                    continue
                if time.monotonic() - start >= timeout_sec:
                    raise TimeoutError(f"Timed out acquiring file lock for {task_dir}")
                time.sleep(_LOCK_POLL_SEC)
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if os.path.exists(lock_path):
                try:
                    with open(lock_path, "r", encoding="utf-8") as handle:
                        current_owner = handle.read().strip()
                except OSError:
                    current_owner = ""
                if current_owner == owner:
                    os.remove(lock_path)
        except OSError:
            pass
        thread_lock.release()


def load_task_info(task_dir: str, raise_error: bool = False) -> Dict[str, Any]:
    """Load task_info.json from a task directory."""
    info_path = os.path.join(task_dir, TASK_INFO_FILENAME)
    try:
        validate_task_directory(task_dir)
        if not os.path.exists(info_path):
            if raise_error:
                raise FileNotFoundError(info_path)
            return {}
        for attempt in range(_READ_RETRY_COUNT):
            try:
                _validate_contained_path(info_path, task_dir, label=TASK_INFO_FILENAME)
                info = _load_json_object(
                    info_path,
                    max_bytes=MAX_TASK_INFO_BYTES,
                    label=TASK_INFO_FILENAME,
                )
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


def save_task_info(task_dir: str, info: Dict[str, Any]) -> None:
    """Save task_info.json atomically after normalizing run-slot fields."""
    validate_task_directory(task_dir)
    os.makedirs(task_dir, exist_ok=True)
    info_path = os.path.join(task_dir, TASK_INFO_FILENAME)
    payload = copy.deepcopy(info)
    payload.pop("id", None)
    normalize_run_history(payload)
    with task_info_lock(task_dir):
        _write_task_info_unlocked(info_path, task_dir, payload)


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
    info = load_task_info(task_dir)
    return extract_metrics(info)


def update_task_info(
    task_dir: str,
    updater: Callable[[Dict[str, Any]], None],
    *,
    timeout_sec: float = _LOCK_TIMEOUT_SEC,
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
        updater(info)
        payload = copy.deepcopy(info)
        payload.pop("id", None)
        normalize_run_history(payload)
        _write_task_info_unlocked(info_path, task_dir, payload)
        return payload


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
    while len(meta["records"]) < target:
        meta["records"].append({})
    while len(meta["tracks"]) < target:
        meta["tracks"].append({})

    meta["run_index"] = max(int(meta.get("run_index", 0) or 0), target)
    meta.pop("_run_index", None)
    return target - 1


def get_log_options(task_dir: str) -> Dict[str, str]:
    """Return ``{display_name: file_path}`` for all available log files."""
    opts: Dict[str, str] = {}
    try:
        run_dir = _task_log_directory(task_dir, create=False)
    except ValueError:
        return opts
    if os.path.isdir(run_dir):
        queue_path = os.path.join(run_dir, QUEUE_LOG_FILENAME)
        if (
            os.path.isfile(queue_path)
            and not _path_is_link_or_reparse(queue_path)
            and _path_is_within(queue_path, run_dir)
        ):
            opts[QUEUE_LOG_FILENAME] = queue_path

        files = sorted(
            [
                f
                for f in os.listdir(run_dir)
                if f.startswith("run") and f.endswith(".log")
                and os.path.isfile(os.path.join(run_dir, f))
                and not _path_is_link_or_reparse(os.path.join(run_dir, f))
                and _path_is_within(os.path.join(run_dir, f), run_dir)
            ],
            key=lambda x: int("".join(filter(str.isdigit, x)) or "0"),
        )
        for f in files:
            opts[f] = os.path.join(run_dir, f)

        err_path = os.path.join(run_dir, ERROR_LOG_FILENAME)
        if (
            os.path.isfile(err_path)
            and not _path_is_link_or_reparse(err_path)
            and _path_is_within(err_path, run_dir)
        ):
            opts[ERROR_LOG_FILENAME] = err_path

    return opts


def resolve_log_path(task_dir: str, log_file_name: Optional[str] = None) -> Optional[str]:
    """Resolve which log file to display for a task."""
    opts = get_log_options(task_dir)
    if log_file_name:
        return opts.get(log_file_name)
    if opts:
        info = load_task_info(task_dir) or {}
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
    meta["records"] = records[:total]
    meta["tracks"] = tracks[:total]
    meta["run_index"] = total
    meta.pop("_run_index", None)
    return total


def _write_task_info_unlocked(info_path: str, task_dir: str, payload: Dict[str, Any]) -> None:
    """Write task info atomically; caller must already hold task_info_lock()."""
    serialized = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
    if len(serialized.encode("utf-8")) > MAX_TASK_INFO_BYTES:
        raise ValueError(
            f"{TASK_INFO_FILENAME} is too large (max {MAX_TASK_INFO_BYTES} bytes): {info_path}"
        )
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
