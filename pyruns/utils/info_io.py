"""Task-level I/O helpers shared by core, UI, and public APIs."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Optional

from pyruns._config import RUN_LOGS_DIR, SCRIPT_INFO_FILENAME, TASK_INFO_FILENAME

_TASK_FILE_LOCKS: Dict[str, threading.RLock] = {}
_TASK_FILE_LOCKS_GUARD = threading.Lock()
_LOCK_FILENAME = f".{TASK_INFO_FILENAME}.lock"
_LOCK_POLL_SEC = 0.05
_LOCK_TIMEOUT_SEC = 5.0


def _thread_lock_for(task_dir: str) -> threading.RLock:
    key = os.path.abspath(task_dir)
    with _TASK_FILE_LOCKS_GUARD:
        lock = _TASK_FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _TASK_FILE_LOCKS[key] = lock
        return lock


@contextmanager
def task_info_lock(task_dir: str, timeout_sec: float = _LOCK_TIMEOUT_SEC):
    """Acquire a task-local thread/process lock for task_info.json updates."""
    thread_lock = _thread_lock_for(task_dir)
    lock_path = os.path.join(task_dir, _LOCK_FILENAME)
    os.makedirs(task_dir, exist_ok=True)
    acquired = thread_lock.acquire(timeout=timeout_sec)
    if not acquired:
        raise TimeoutError(f"Timed out acquiring task lock for {task_dir}")

    fd: Optional[int] = None
    start = time.monotonic()
    try:
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(fd, f"{os.getpid()} {threading.get_ident()}".encode("utf-8", errors="ignore"))
                break
            except FileExistsError:
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
                os.remove(lock_path)
        except OSError:
            pass
        thread_lock.release()


def load_task_info(task_dir: str, raise_error: bool = False) -> Dict[str, Any]:
    """Load task_info.json from a task directory."""
    info_path = os.path.join(task_dir, TASK_INFO_FILENAME)
    if not os.path.exists(info_path):
        return {}
    try:
        with open(info_path, "r", encoding="utf-8") as f:
            info = json.load(f)
    except Exception:
        if raise_error:
            raise
        return {}

    info.pop("id", None)
    normalize_run_history(info)
    return info


def save_task_info(task_dir: str, info: Dict[str, Any]) -> None:
    """Save task_info.json atomically after normalizing run-slot fields."""
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
    if not os.path.exists(script_info_path):
        return {}
    try:
        with open(script_info_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_script_info(run_root: str, info: Dict[str, Any]) -> None:
    """Save script_info.json atomically to the run root directory."""
    os.makedirs(run_root, exist_ok=True)
    script_info_path = os.path.join(run_root, SCRIPT_INFO_FILENAME)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{SCRIPT_INFO_FILENAME}.",
        suffix=".tmp",
        dir=run_root,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, script_info_path)
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
    raise_error: bool = False,
) -> Dict[str, Any]:
    """Read-modify-write task_info.json using the shared atomic save path."""
    info_path = os.path.join(task_dir, TASK_INFO_FILENAME)
    with task_info_lock(task_dir):
        if os.path.exists(info_path):
            try:
                with open(info_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
            except Exception:
                if raise_error:
                    raise
                info = {}
        else:
            if raise_error:
                raise FileNotFoundError(info_path)
            info = {}

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
    return max(
        len(list(meta.get("start_times", []) or [])),
        len(list(meta.get("finish_times", []) or [])),
        len(list(meta.get("pids", []) or [])),
        len(list(meta.get("records", []) or [])),
        len(list(meta.get("tracks", []) or [])),
        int(meta.get("run_index", meta.get("_run_index", 0)) or 0),
    )


def ensure_run_slot(meta: Dict[str, Any], run_index: int) -> int:
    """Pad run arrays so that *run_index* exists and return the zero-based slot."""
    target = max(int(run_index or 0), 1)
    meta["start_times"] = list(meta.get("start_times", []) or [])
    meta["finish_times"] = list(meta.get("finish_times", []) or [])
    meta["pids"] = list(meta.get("pids", []) or [])
    meta["records"] = list(meta.get("records", []) or [])
    meta["tracks"] = list(meta.get("tracks", []) or [])

    while len(meta["start_times"]) < target:
        meta["start_times"].append("")
    while len(meta["finish_times"]) < target:
        meta["finish_times"].append("")
    while len(meta["pids"]) < target:
        meta["pids"].append(None)
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
    run_dir = os.path.join(task_dir, RUN_LOGS_DIR)
    if os.path.isdir(run_dir):
        files = sorted(
            [
                f
                for f in os.listdir(run_dir)
                if f.startswith("run") and f.endswith(".log")
            ],
            key=lambda x: int("".join(filter(str.isdigit, x)) or "0"),
        )
        for f in files:
            opts[f] = os.path.join(run_dir, f)

        from pyruns._config import ERROR_LOG_FILENAME

        err_path = os.path.join(run_dir, ERROR_LOG_FILENAME)
        if os.path.exists(err_path):
            opts[ERROR_LOG_FILENAME] = err_path

    return opts


def resolve_log_path(task_dir: str, log_file_name: Optional[str] = None) -> Optional[str]:
    """Resolve which log file to display for a task."""
    opts = get_log_options(task_dir)
    if log_file_name:
        return opts.get(log_file_name)
    if opts:
        run_logs = {
            name: path
            for name, path in opts.items()
            if name.startswith("run") and name.endswith(".log")
        }
        candidates = run_logs or opts
        cached = [(f, p, os.path.getmtime(p)) for f, p in candidates.items()]
        cached.sort(key=lambda x: x[2], reverse=True)
        return cached[0][1]
    return None


_INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def validate_task_name(name: str, root_dir: Optional[str] = None) -> Optional[str]:
    """Validate whether a task name can be used as a folder name."""
    if not name or not name.strip():
        return "Task name cannot be empty"
    name = name.strip()
    if len(name) > 200:
        return "Task name is too long (max 200 characters)"
    bad = _INVALID_CHARS_RE.findall(name)
    if bad:
        return f"Task name contains invalid characters: {''.join(set(bad))}"
    if name.startswith("."):
        return "Task name cannot start with '.'"

    if root_dir and os.path.exists(os.path.join(root_dir, name)):
        return f"Task name '{name}' already exists in the current workspace"
    return None


def normalize_run_history(meta: Dict[str, Any]) -> int:
    """Align run-slot arrays without discarding failed or incomplete runs."""
    total = run_slot_count(meta)

    starts = list(meta.get("start_times", []) or [])
    finishes = list(meta.get("finish_times", []) or [])
    pids = list(meta.get("pids", []) or [])
    records = list(meta.get("records", []) or [])
    tracks = list(meta.get("tracks", []) or [])

    while len(starts) < total:
        starts.append("")
    while len(finishes) < total:
        finishes.append("")
    while len(pids) < total:
        pids.append(None)
    while len(records) < total:
        records.append({})
    while len(tracks) < total:
        tracks.append({})

    meta["start_times"] = starts[:total]
    meta["finish_times"] = finishes[:total]
    meta["pids"] = pids[:total]
    meta["records"] = records[:total]
    meta["tracks"] = tracks[:total]
    meta["run_index"] = total
    meta.pop("_run_index", None)
    return total


def _write_task_info_unlocked(info_path: str, task_dir: str, payload: Dict[str, Any]) -> None:
    """Write task info atomically; caller must already hold task_info_lock()."""
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{TASK_INFO_FILENAME}.",
        suffix=".tmp",
        dir=task_dir,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, info_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
