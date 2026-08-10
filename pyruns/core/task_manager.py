"""Task registry, disk sync, and background scheduling for Pyruns."""

from __future__ import annotations

import atexit
import copy
import dataclasses
import os
import re
import secrets
import socket
import threading
import time
import uuid
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from pyruns._config import (
    DEFAULT_RUNNER_HEARTBEAT_SECONDS,
    DEFAULT_RUNNER_LEASE_SECONDS,
    ERROR_LOG_FILENAME,
    EXECUTION_MODES,
    QUEUE_LOG_FILENAME,
    TASK_KIND_CONFIG,
    TASK_INFO_FILENAME,
    TASKS_DIR,
    TRASH_DIR,
)
from pyruns.core.executor import _load_workspace_global_env, run_task_worker
from pyruns.core.gpu_scheduler import (
    GpuAssignment,
    GpuDecision,
    GpuResourceScheduler,
    GpuSchedulerConfig,
    format_gpu_queue_block,
    format_gpu_rule,
)
from pyruns.utils import get_logger, get_now_str
from pyruns.utils.info_io import (
    MAX_RUN_HISTORY_SLOTS,
    ensure_run_slot,
    load_task_info,
    prepare_task_log_path,
    run_slot_count,
    task_info_lock,
    update_task_info,
    validate_task_directory,
    validate_task_name,
    validate_tasks_root,
)
from pyruns.utils.process_utils import (
    get_process_create_time,
    is_pid_running,
    kill_process,
    process_identity_matches,
)
from pyruns.utils.settings import load_settings
from pyruns.utils.env_utils import normalize_environment
from pyruns.utils.events import event_sys
from pyruns.utils.task_files import (
    build_task_preview_and_search,
    normalize_task_kind,
    read_task_payload,
    resolve_task_config_file,
)

logger = get_logger(__name__)
_STOP_TASK_INFO_LOCK_TIMEOUT_SEC = 1.0
_ACTIVE_DELETE_SETTLE_TIMEOUT_SEC = 15.0
_GPU_SCHEDULE_LOCK_TIMEOUT_SEC = 2.0
_REACTIVE_DISK_REFRESH_INTERVAL_SEC = 1.0
_NAMESPACE_OPERATION_KEY = "_namespace_operation"
_NAMESPACE_OPERATION_LEASE_SEC = 60.0
_GPU_QUEUE_RUN_RE = re.compile(r"\bRun #(\d+)\b")
_GPU_WAIT_REASON_NORMALIZERS = (
    (re.compile(r"\bstabilizing\s+\d+(?:\.\d+)?/(\d+(?:\.\d+)?s)\b"), r"stabilizing /\1"),
    (re.compile(r"\bmemory\s+\d+(?:\.\d+)?%\s*(>\s*\d+(?:\.\d+)?%)"), r"memory \1"),
    (re.compile(r"\bfree\s+\d+(?:\.\d+)?\s+GiB\s*(<\s*\d+(?:\.\d+)?\s+GiB)"), r"free \1"),
    (re.compile(r"\bcompute\s+\d+(?:\.\d+)?%\s*(>\s*\d+(?:\.\d+)?%)"), r"compute \1"),
)


def active_task_run_index(info: Dict[str, Any]) -> int:
    """Return the run number represented by one active task snapshot."""

    status = str(info.get("status", "") or "").lower()
    try:
        current = int(info.get("run_index", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        current = 0
    if status != "queued":
        return max(current, run_slot_count(info))

    queued = run_slot_count(info) + 1
    gpu_wait = info.get("gpu_wait")
    if isinstance(gpu_wait, dict):
        try:
            queued = max(queued, int(gpu_wait.get("run_index", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            pass
    return queued


class TaskClaimConflict(RuntimeError):
    """Raised when another runner already owns a live task lease."""


class TaskStateConflict(RuntimeError):
    """Raised when disk state changed before a local state transition landed."""


class TaskManager:
    """Central task registry, scheduler, and UI notification source."""

    def __init__(
        self,
        tasks_dir: str | None = None,
        lazy_scan: bool | None = True,
        runner_token: str | None = None,
        *,
        owns_task_lifecycle: bool = True,
    ):
        if tasks_dir is None:
            from pyruns._config import ROOT_DIR

            tasks_dir = os.path.join(ROOT_DIR, TASKS_DIR)

        validate_tasks_root(tasks_dir)
        self.tasks_dir = tasks_dir
        self.tasks: List[Dict[str, Any]] = []
        self._tasks_by_name: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._observer_lock = threading.Lock()
        self._executor_lock = threading.Lock()
        self._shutdown_lock = threading.RLock()
        self._shutdown_event = threading.Event()
        self._shutdown_cleanup_done = False
        self.owns_task_lifecycle = bool(owns_task_lifecycle)

        self._observers: List[Callable[[], None]] = []
        self._reactive_watchers = 0
        self._executor = None
        self._independent_executor = None
        self._independent_executor_mode = None
        self._executor_mode = None
        self._executor_workers = 0
        self.runner_host = socket.gethostname().lower()
        token = str(runner_token or uuid.uuid4().hex[:8])
        self.runner_id = f"{self.runner_host}:{os.getpid()}:{token}"
        self.lease_seconds = DEFAULT_RUNNER_LEASE_SECONDS
        self._last_queued_lease_heartbeat = 0.0
        self._atexit_callback = self.shutdown
        self._atexit_registered = False

        self.execution_mode = "thread"
        self.max_workers = 1
        self.is_processing = False
        self._running_ids: set[str] = set()
        self._batch_running_ids: set[str] = set()
        self._disk_scan_complete = False
        self.gpu_scheduler = GpuResourceScheduler()

        logger.info("TaskManager initialised  root=%s", tasks_dir)
        if lazy_scan is None:
            pass
        elif lazy_scan:
            self.scan_disk_async()
        else:
            self.scan_disk()

        self._scheduler_thread: threading.Thread | None = None
        if self.owns_task_lifecycle:
            self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
            self._scheduler_thread.start()
            atexit.register(self._atexit_callback)
            self._atexit_registered = True

    def on_change(self, callback: Callable[[], None]) -> None:
        """Register a callback used by reactive UI pages."""
        with self._observer_lock:
            if callback not in self._observers:
                self._observers.append(callback)

    def off_change(self, callback: Callable[[], None]) -> None:
        """Unregister a previously registered callback."""
        with self._observer_lock:
            if callback in self._observers:
                self._observers.remove(callback)

    def acquire_reactive_watch(self) -> None:
        """Request fast disk reconciliation while a live UI is connected."""
        with self._observer_lock:
            self._reactive_watchers += 1

    def release_reactive_watch(self) -> None:
        """Release one live UI disk-reconciliation request."""
        with self._observer_lock:
            self._reactive_watchers = max(0, self._reactive_watchers - 1)

    def has_reactive_watchers(self) -> bool:
        """Return whether any live UI currently needs near-real-time discovery."""
        with self._observer_lock:
            return self._reactive_watchers > 0

    def _mark_running_locked(self, task_name: str, *, counts_for_batch: bool) -> None:
        if not task_name:
            return
        self._running_ids.add(task_name)
        if counts_for_batch:
            self._batch_running_ids.add(task_name)
        else:
            self._batch_running_ids.discard(task_name)

    def _clear_running_locked(self, task_name: str) -> None:
        if not task_name:
            return
        self._running_ids.discard(task_name)
        self._batch_running_ids.discard(task_name)

    def _clear_running_many_locked(self, task_names: set[str]) -> None:
        self._running_ids.difference_update(task_names)
        self._batch_running_ids.difference_update(task_names)

    def trigger_update(self) -> None:
        """Notify all current observers."""
        with self._observer_lock:
            callbacks = list(self._observers)

        for callback in callbacks:
            try:
                callback()
            except Exception as exc:
                logger.error("Observer callback error: %s", exc)

    @staticmethod
    def _serialized_gpu_wait(task: Dict[str, Any]) -> Dict[str, Any] | None:
        if str(task.get("status", "") or "").lower() != "queued":
            return None
        raw = task.get("gpu_wait")
        if not isinstance(raw, dict):
            return None
        wait = copy.deepcopy(raw)
        try:
            started_at = float(wait.get("started_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            started_at = 0.0
        if started_at > 0:
            waited = max(0.0, time.time() - started_at)
            wait["waited_seconds"] = waited
            try:
                deadline = float(wait.get("deadline_at", 0.0) or 0.0)
            except (TypeError, ValueError):
                deadline = 0.0
            if deadline <= 0:
                try:
                    max_wait = float(wait.get("max_wait_seconds", 0.0) or 0.0)
                except (TypeError, ValueError):
                    max_wait = 0.0
                deadline = started_at + max(0.0, max_wait)
                wait["deadline_at"] = deadline
            wait["remaining_seconds"] = max(0.0, deadline - time.time())
        return wait

    _SUMMARY_API_FIELDS = (
        "dir",
        "name",
        "status",
        "created_at",
        "config_file",
        "progress",
        "env",
        "pinned",
        "task_order",
        "script",
        "task_kind",
        "command_mode",
        "cmd",
        "workdir",
        "shell_executable",
        "shell_kind",
        "start_times",
        "finish_times",
        "pids",
        "pid_create_times",
        "run_statuses",
        "durations",
        "exit_codes",
        "source_states",
        "notes",
        "run_index",
        "queued_at",
        "gpu_wait",
        "preview_text",
        "search_text",
        "_load_error",
    )

    @classmethod
    def _snapshot_task_for_api(
        cls,
        task: Dict[str, Any] | None,
        *,
        summary: bool,
    ) -> Dict[str, Any] | None:
        """Capture a stable detached task value while the manager lock is held."""
        if task is None:
            return None
        if not summary:
            return copy.deepcopy(task)
        return {
            key: copy.deepcopy(task[key])
            for key in cls._SUMMARY_API_FIELDS
            if key in task
        }

    @staticmethod
    def _finalize_full_task_snapshot(data: Dict[str, Any]) -> Dict[str, Any]:
        """Apply derived API fields to an already detached full-task snapshot."""
        data["dir"] = str(data.get("dir", "")).replace("\\", "/")
        data.pop("_gpu_wait_persisted_signature", None)
        gpu_wait = TaskManager._serialized_gpu_wait(data)
        if gpu_wait is None:
            data.pop("gpu_wait", None)
        else:
            data["gpu_wait"] = gpu_wait
        return data

    @staticmethod
    def serialize_task(task: Dict[str, Any] | None, *, summary: bool = False) -> Dict[str, Any] | None:
        """Return a detached task copy suitable for APIs and read-only consumers."""
        if task is None:
            return None
        if summary:
            return {
                "dir": str(task.get("dir", "")).replace("\\", "/"),
                "name": task.get("name", ""),
                "status": task.get("status", "pending"),
                "created_at": task.get("created_at"),
                "config": {},
                "config_text": "",
                "config_file": task.get("config_file", ""),
                "log": "",
                "progress": task.get("progress", 0.0),
                "env": dict(task.get("env", {}) or {}),
                "pinned": task.get("pinned", False),
                "task_order": task.get("task_order"),
                "script": task.get("script"),
                "task_kind": task.get("task_kind"),
                "command_mode": task.get("command_mode"),
                "cmd": copy.deepcopy(task.get("cmd")),
                "workdir": task.get("workdir"),
                "shell_executable": task.get("shell_executable"),
                "shell_kind": task.get("shell_kind"),
                "start_times": list(task.get("start_times", []) or []),
                "finish_times": list(task.get("finish_times", []) or []),
                "pids": list(task.get("pids", []) or []),
                "pid_create_times": list(task.get("pid_create_times", []) or []),
                "run_statuses": list(task.get("run_statuses", []) or []),
                "durations": list(task.get("durations", []) or []),
                "exit_codes": list(task.get("exit_codes", []) or []),
                "source_states": list(task.get("source_states", []) or []),
                "records": [],
                "tracks": [],
                "notes": task.get("notes", ""),
                "run_index": task.get("run_index", 0),
                "queued_at": task.get("queued_at"),
                "gpu_wait": TaskManager._serialized_gpu_wait(task),
                "preview_text": task.get("preview_text", ""),
                "search_text": task.get("search_text", ""),
                "_load_error": task.get("_load_error"),
            }
        return TaskManager._finalize_full_task_snapshot(copy.deepcopy(task))

    def list_tasks(self, *, summary: bool = False) -> List[Dict[str, Any]]:
        """Return detached copies of the current task list."""
        with self._lock:
            snapshots = [
                self._snapshot_task_for_api(task, summary=summary)
                for task in self.tasks
            ]
        return [
            serialized
            for serialized in (
                self.serialize_task(snapshot, summary=True)
                if summary
                else self._finalize_full_task_snapshot(snapshot)
                for snapshot in snapshots
                if snapshot is not None
            )
            if serialized is not None
        ]

    def get_task(self, identifier: str) -> Dict[str, Any] | None:
        """Return a detached task copy by name."""
        with self._lock:
            snapshot = self._snapshot_task_for_api(
                self._tasks_by_name.get(identifier),
                summary=False,
            )
        return self._finalize_full_task_snapshot(snapshot) if snapshot is not None else None

    def scan_disk_async(self) -> None:
        """Run a full disk scan in the background."""

        def _job() -> None:
            self.scan_disk()
            self.trigger_update()

        threading.Thread(target=_job, daemon=True).start()

    def scan_disk(self) -> None:
        """Fully rebuild the in-memory task list from disk."""
        if not self.tasks_dir or not os.path.exists(self.tasks_dir):
            logger.warning("root_dir does not exist: %s", self.tasks_dir)
            with self._lock:
                self.tasks = []
                self._rebuild_indexes_locked()
                self.is_processing = False
                self._disk_scan_complete = True
            return

        scan_ok, subdirs = self._scan_task_dir_names()
        if not scan_ok:
            logger.debug("scan_disk skipped; could not list task directories under %s", self.tasks_dir)
            with self._lock:
                self._disk_scan_complete = True
            return

        # Parallel I/O: load task dirs concurrently for large workspaces
        if len(subdirs) > 8:
            with ThreadPoolExecutor(max_workers=min(16, len(subdirs))) as pool:
                results = list(pool.map(self._load_task_dir, subdirs))
            new_tasks = [t for t in results if t is not None]
        else:
            new_tasks = []
            for dir_name in subdirs:
                task = self._load_task_dir(dir_name)
                if task is not None:
                    new_tasks.append(task)

        with self._lock:
            self.tasks = new_tasks
            self._rebuild_indexes_locked()
            self._recompute_processing_flag_locked()
            self._disk_scan_complete = True
        logger.debug("scan_disk completed: %d tasks found", len(new_tasks))

    def _list_task_dir_names(self) -> list[str]:
        """Return task folder names ordered by directory mtime, newest first."""
        _ok, names = self._scan_task_dir_names()
        return names

    def _scan_task_dir_names(self) -> tuple[bool, list[str]]:
        """Try to list task folder names ordered by directory mtime."""
        if not self.tasks_dir:
            return True, []

        try:
            validate_tasks_root(self.tasks_dir)
            entries = []
            with os.scandir(self.tasks_dir) as it:
                for entry in it:
                    # Task names cannot start with '.', so hidden directories are
                    # always Pyruns internals (for example transactional staging)
                    # or foreign metadata. Never surface them as corrupt tasks.
                    if not entry.name.startswith(".") and entry.name != TRASH_DIR:
                        try:
                            validate_task_directory(entry.path)
                        except ValueError as exc:
                            logger.warning("Ignoring unsafe task directory %s: %s", entry.path, exc)
                            continue
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        try:
                            mtime_ns = entry.stat().st_mtime_ns
                        except OSError:
                            mtime_ns = 0
                        entries.append((entry.name, mtime_ns))
            entries.sort(key=lambda x: x[1], reverse=True)
            return True, [name for name, _ in entries]
        except (OSError, ValueError) as exc:
            logger.debug("Could not list task directories under %s: %s", self.tasks_dir, exc)
            return False, []

    def sync_task_dirs_from_disk(self) -> bool:
        """Discover task folders created or removed by another Pyruns process."""
        if not self.tasks_dir or not os.path.exists(self.tasks_dir):
            with self._lock:
                had_tasks = bool(self.tasks)
                self.tasks = []
                self._running_ids.clear()
                self._batch_running_ids.clear()
                self._rebuild_indexes_locked()
                self._recompute_processing_flag_locked()
                self._disk_scan_complete = True
            return had_tasks

        scan_ok, disk_names = self._scan_task_dir_names()
        if not scan_ok:
            return False

        disk_name_set = set(disk_names)
        with self._lock:
            known_names = set(self._tasks_by_name)

        missing_names = known_names - disk_name_set
        new_names = [name for name in disk_names if name not in known_names]
        if not missing_names and not new_names:
            return False

        if len(new_names) > 8:
            with ThreadPoolExecutor(max_workers=min(16, len(new_names))) as pool:
                results = list(pool.map(self._load_task_dir, new_names))
            new_tasks = [task for task in results if task is not None]
        else:
            new_tasks = [
                task
                for task in (self._load_task_dir(name) for name in new_names)
                if task is not None
            ]

        changed = False
        with self._lock:
            if missing_names:
                before_count = len(self.tasks)
                self.tasks = [
                    task
                    for task in self.tasks
                    if task and task.get("name") not in missing_names
                ]
                self._clear_running_many_locked(missing_names)
                changed = changed or len(self.tasks) != before_count
                self._rebuild_indexes_locked()

            current_names = set(self._tasks_by_name)
            for task in reversed(new_tasks):
                task_name = str(task.get("name", "") or "")
                if task_name and task_name not in current_names:
                    self.tasks.insert(0, task)
                    current_names.add(task_name)
                    changed = True

            if changed:
                disk_order = {name: index for index, name in enumerate(disk_names)}
                self.tasks.sort(
                    key=lambda task: disk_order.get(
                        str((task or {}).get("name", "") or ""),
                        len(disk_order),
                    )
                )
                self._rebuild_indexes_locked()
                self._recompute_processing_flag_locked()
                self._disk_scan_complete = True
        return changed

    @staticmethod
    def _lease_until_value(info: Dict[str, Any]) -> float:
        try:
            return float(info.get("lease_until", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _lease_active(cls, info: Dict[str, Any], *, now: float | None = None) -> bool:
        return cls._lease_until_value(info) > (time.time() if now is None else now)

    def _is_foreign_live_runner(self, info: Dict[str, Any]) -> bool:
        runner_id = str(info.get("runner_id", "") or "")
        return bool(runner_id and runner_id != self.runner_id and self._lease_active(info))

    def _is_local_or_legacy_runner(self, info: Dict[str, Any]) -> bool:
        runner_host = str(info.get("runner_host", "") or "").lower()
        return not runner_host or runner_host == self.runner_host

    def _is_current_runner(self, info: Dict[str, Any]) -> bool:
        return str(info.get("runner_id", "") or "") == self.runner_id

    def _should_kill_task_process(self, info: Dict[str, Any] | None) -> bool:
        """Only terminate processes this TaskManager explicitly claimed."""

        if not info:
            return False
        return self._is_current_runner(info)

    def _running_info_has_live_owner(self, info: Dict[str, Any]) -> bool:
        pid, created_at = self._current_process_identity(info)
        foreign_runner_live = self._is_foreign_live_runner(info)
        current_runner_live = bool(
            self._is_current_runner(info)
            and pid
            and (
                process_identity_matches(pid, created_at)
                if created_at is not None
                else is_pid_running(pid)
            )
        )
        return bool(foreign_runner_live or current_runner_live)

    def _fail_unowned_running_info_if_needed(
        self,
        task_name: str,
        task_dir: str,
        info: Dict[str, Any],
    ) -> tuple[Dict[str, Any], bool]:
        if (
            not self.owns_task_lifecycle
            or info.get("status") != "running"
            or task_name in self._running_ids
        ):
            return info, False
        if self._running_info_has_live_owner(info):
            return info, False

        try:
            self._mark_failed_on_disk(
                {"name": task_name, "dir": task_dir, "run_index": run_slot_count(info)},
                expected_statuses={"running"},
                require_no_live_owner=True,
            )
        except (TaskClaimConflict, TaskStateConflict):
            updated = load_task_info(task_dir) or info
            return updated, False
        updated = load_task_info(task_dir) or info
        logger.warning("%s: running lease is not trusted or process is gone; marked failed", task_name)
        return updated, True

    @staticmethod
    def _same_task_dir(left: str | None, right: str | None) -> bool:
        if not left or not right:
            return False
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))

    @staticmethod
    def _directory_identity(path: str) -> tuple[int, int] | None:
        """Return the identity of one directory without following a link."""

        try:
            value = os.lstat(path)
        except OSError:
            return None
        return int(value.st_dev), int(value.st_ino)

    def _namespace_operation_is_live(self, operation: Any) -> bool:
        """Return whether a task-directory move marker still has a live owner."""

        if not isinstance(operation, dict):
            return False
        try:
            expires_at = float(operation.get("expires_at", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            return False
        if expires_at <= time.time():
            return False

        host = str(operation.get("host", "") or "").lower()
        if host and host != self.runner_host:
            return True
        try:
            pid = int(operation.get("pid", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        if pid <= 0 or not is_pid_running(pid):
            return False
        expected_create_time = operation.get("pid_create_time")
        if expected_create_time is None:
            return True
        return process_identity_matches(pid, expected_create_time)

    def _guard_namespace_operation(self, info: Dict[str, Any]) -> None:
        """Reject a live directory move and discard a stale marker atomically."""

        operation = info.get(_NAMESPACE_OPERATION_KEY)
        if operation is None:
            return
        if self._namespace_operation_is_live(operation):
            kind = str(operation.get("kind", "move") or "move")
            raise TaskStateConflict(f"task directory is being prepared for {kind}")
        info.pop(_NAMESPACE_OPERATION_KEY, None)

    def _begin_namespace_operation(
        self,
        task_dir: str,
        task_name: str,
        *,
        kind: str,
        expected_run_index: int,
    ) -> str:
        """Reserve one inactive task directory against concurrent reruns."""

        token = secrets.token_hex(16)
        operation = {
            "kind": str(kind),
            "token": token,
            "host": self.runner_host,
            "pid": os.getpid(),
            "pid_create_time": get_process_create_time(os.getpid()),
            "expires_at": time.time() + _NAMESPACE_OPERATION_LEASE_SEC,
        }

        def _apply(info: Dict[str, Any]) -> None:
            self._guard_namespace_operation(info)
            if str(info.get("name", "") or "") != task_name:
                raise TaskStateConflict("task name changed before directory move")
            status = str(info.get("status", "") or "").lower()
            if status in {"queued", "running"}:
                raise TaskStateConflict("task became active before directory move")
            if active_task_run_index(info) != int(expected_run_index):
                raise TaskStateConflict("task run changed before directory move")
            info[_NAMESPACE_OPERATION_KEY] = operation

        update_task_info(task_dir, _apply)
        return token

    @staticmethod
    def _finish_namespace_operation(
        task_dir: str,
        token: str,
        *,
        new_name: str | None = None,
    ) -> Dict[str, Any]:
        """Clear only the caller's move marker and optionally commit a new name."""

        def _apply(info: Dict[str, Any]) -> None:
            operation = info.get(_NAMESPACE_OPERATION_KEY)
            if not isinstance(operation, dict) or operation.get("token") != token:
                raise TaskStateConflict("task directory move marker changed")
            if new_name is not None:
                info["name"] = new_name
            info.pop(_NAMESPACE_OPERATION_KEY, None)

        return update_task_info(task_dir, _apply)

    @staticmethod
    def _rollback_namespace_operation(task_dir: str, token: str) -> None:
        """Best-effort cleanup after a task-directory move does not complete."""

        def _apply(info: Dict[str, Any]) -> None:
            operation = info.get(_NAMESPACE_OPERATION_KEY)
            if isinstance(operation, dict) and operation.get("token") == token:
                info.pop(_NAMESPACE_OPERATION_KEY, None)

        try:
            update_task_info(task_dir, _apply)
        except (FileNotFoundError, OSError, TimeoutError, TypeError, ValueError):
            pass

    def _refresh_memory_task_from_disk_info(
        self,
        task_name: str,
        task_dir: str,
        info: Dict[str, Any],
    ) -> None:
        """Refresh one in-memory task after a disk-side state race is detected."""

        with self._lock:
            current = self._tasks_by_name.get(task_name)
            if not current or not self._same_task_dir(str(current.get("dir", "") or ""), task_dir):
                return
            self._apply_info_to_task(current, info)
            status = str(current.get("status", "") or "").lower()
            if status != "running" or not self._is_current_runner(info):
                self._clear_running_locked(task_name)
                self.gpu_scheduler.release(task_name)
            self._recompute_processing_flag_locked()

    def _load_task_dir(self, dir_name: str) -> Dict[str, Any] | None:
        """Load one task folder into the normalized task dict shape."""
        if validate_task_name(dir_name) is not None:
            return None
        task_dir = os.path.join(self.tasks_dir, dir_name)
        info_path = os.path.join(task_dir, TASK_INFO_FILENAME)
        if not os.path.exists(info_path):
            return None

        metadata_error = ""
        try:
            info = load_task_info(task_dir, raise_error=True)
            if not info:
                metadata_error = "Task metadata is empty"
        except Exception as exc:
            metadata_error = f"Could not load task metadata: {exc}"
            logger.error("Error loading info for %s: %s", dir_name, exc)
            info = {}
        if info:
            info = self._strip_queued_placeholder_run(info)

        task_kind, config_data, config_text, payload_error = read_task_payload(task_dir, info)
        load_error = "; ".join(
            message for message in (metadata_error, payload_error) if message
        )

        task_name = dir_name
        if info:
            info, _ = self._fail_unowned_running_info_if_needed(task_name, task_dir, info)

        try:
            mtime_ns = os.stat(info_path).st_mtime_ns
        except OSError:
            mtime_ns = 0

        task = {
            "dir": task_dir.replace("\\", "/"),
            "name": task_name,
            "status": info.get("status", "failed" if metadata_error else "pending"),
            "created_at": info.get("created_at"),
            "config": config_data,
            "config_text": config_text,
            "config_file": resolve_task_config_file(info, task_kind or None, task_dir),
            "log": "",
            "progress": info.get("progress", 0.0),
            "env": info.get("env", {}),
            "pinned": info.get("pinned", False),
            "task_order": info.get("task_order"),
            "script": info.get("script"),
            "task_kind": task_kind or normalize_task_kind(info.get("task_kind", info.get("config_mode"))),
            "command_mode": info.get("command_mode"),
            "cmd": info.get("cmd"),
            "workdir": info.get("workdir"),
            "shell_executable": info.get("shell_executable"),
            "shell_kind": info.get("shell_kind"),
            "start_times": info.get("start_times", []),
            "finish_times": info.get("finish_times", []),
            "pids": info.get("pids", []),
            "pid_create_times": info.get("pid_create_times", []),
            "run_statuses": info.get("run_statuses", []),
            "durations": info.get("durations", []),
            "exit_codes": info.get("exit_codes", []),
            "source_states": info.get("source_states", []),
            "records": info.get("records", []),
            "tracks": info.get("tracks", []),
            "notes": info.get("notes", ""),
            "runner_id": info.get("runner_id"),
            "runner_host": info.get("runner_host"),
            "lease_until": info.get("lease_until"),
            "lease_heartbeat": info.get("lease_heartbeat"),
            "_load_error": load_error,
            "_mtime": (mtime_ns / 1_000_000_000) if mtime_ns else 0.0,
            "_mtime_ns": mtime_ns,
        }
        pending_run_index = info.get("run_index", info.get("_run_index"))
        if pending_run_index:
            task["run_index"] = int(pending_run_index)
        self._copy_gpu_schedule_info(task, info)
        self._copy_gpu_wait_info(task, info)
        self._refresh_derived_fields(task)
        return task

    def refresh_from_disk(
        self,
        task_ids: List[str] | None = None,
        force_all: bool = False,
        check_all: bool = False,
        discover: bool = False,
    ) -> bool:
        """Refresh active or requested tasks from task_info.json files."""
        has_changed = self.sync_task_dirs_from_disk() if discover else False

        with self._lock:
            current = list(self.tasks)

        target_ids = set(task_ids) if task_ids else None
        for task in current:
            if not task:
                continue
            if not (
                force_all
                or check_all
                or (target_ids and self._task_matches_identifier(task, target_ids))
                or task["status"] in ("running", "queued")
            ):
                continue

            info_path = os.path.join(task["dir"], TASK_INFO_FILENAME)
            try:
                mtime_ns = os.stat(info_path).st_mtime_ns
                if not force_all and task.get("_mtime_ns") == mtime_ns:
                    if task.get("status") == "running" and task.get("name") not in self._running_ids:
                        info = load_task_info(task["dir"])
                        if not info:
                            continue
                        info = self._strip_queued_placeholder_run(info)
                        info, failed = self._fail_unowned_running_info_if_needed(task["name"], task["dir"], info)
                        if failed:
                            try:
                                mtime_ns = os.stat(info_path).st_mtime_ns
                            except OSError:
                                mtime_ns = 0
                            with self._lock:
                                existing = self._tasks_by_name.get(task["name"])
                                if existing:
                                    before = self._task_snapshot(existing)
                                    self._apply_info_to_task(existing, info, mtime_ns=mtime_ns)
                                    self._clear_running_locked(task["name"])
                                    self.gpu_scheduler.release(task["name"])
                                    self._recompute_processing_flag_locked()
                                    after = self._task_snapshot(existing)
                                    has_changed |= before != after
                    continue
                info = load_task_info(task["dir"])
                if not info:
                    continue
                info = self._strip_queued_placeholder_run(info)

                with self._lock:
                    existing = self._tasks_by_name.get(task["name"])
                    if not existing:
                        continue
                    before = self._task_snapshot(existing)
                    self._apply_info_to_task(existing, info, mtime_ns=mtime_ns)
                    self._recompute_processing_flag_locked()
                    after = self._task_snapshot(existing)
                has_changed |= before != after
            except Exception as exc:
                logger.debug("refresh_from_disk skipped %s: %s", task.get("name"), exc)

        return has_changed

    def load_task_by_name(self, name: str) -> Dict[str, Any] | None:
        """Load one exact task folder without scanning the whole workspace."""
        task_name = str(name or "")
        if validate_task_name(task_name) is not None:
            return None

        task = self._load_task_dir(task_name)
        if task is None:
            return None

        with self._lock:
            existing = self._tasks_by_name.get(task_name)
            if existing:
                existing.clear()
                existing.update(task)
                loaded = existing
            else:
                self.tasks.insert(0, task)
                loaded = task
            self._rebuild_indexes_locked()
            self._recompute_processing_flag_locked()
        return loaded

    def _upsert_task_locked(self, task_obj: Dict[str, Any]) -> None:
        task_name = str((task_obj or {}).get("name", "") or "")
        if not task_name:
            self.tasks.insert(0, task_obj)
            return

        existing = self._tasks_by_name.get(task_name)
        if existing:
            merged = dict(existing)
            merged.update(task_obj)
            existing.clear()
            existing.update(merged)
            task_obj = existing

        self.tasks = [
            task
            for task in self.tasks
            if str((task or {}).get("name", "") or "") != task_name
        ]
        self.tasks.insert(0, task_obj)

    def add_task(self, task_obj: Dict[str, Any]) -> None:
        with self._lock:
            self._upsert_task_locked(task_obj)
            self._rebuild_indexes_locked()
            self._recompute_processing_flag_locked()
        self.trigger_update()

    def add_tasks(self, task_objs: List[Dict[str, Any]]) -> None:
        with self._lock:
            for task in reversed(task_objs):
                self._upsert_task_locked(task)
            self._rebuild_indexes_locked()
            self._recompute_processing_flag_locked()
        self.trigger_update()

    @staticmethod
    def _clear_gpu_schedule_state(task: Dict[str, Any]) -> None:
        for key in (
            "_scheduled_env",
            "_gpu_assignment",
            "gpu_wait",
            "queued_at",
            "_gpu_wait_started_at",
            "_gpu_wait_logged_for",
            "_gpu_last_wait_log_at",
            "_gpu_wait_refresh_width",
            "_gpu_wait_persisted_signature",
            "_queued_independent",
            "_queued_execution_mode",
        ):
            task.pop(key, None)

    @staticmethod
    def _clear_gpu_schedule_info(info: Dict[str, Any]) -> None:
        for key in ("_scheduled_env", "_gpu_assignment"):
            info.pop(key, None)

    @staticmethod
    def _clear_gpu_wait_info(info: Dict[str, Any]) -> None:
        info.pop("gpu_wait", None)
        info.pop("queued_at", None)
        info.pop("_gpu_wait_persisted_signature", None)

    @staticmethod
    def _copy_gpu_wait_info(task: Dict[str, Any], info: Dict[str, Any]) -> None:
        if str(info.get("status", "") or "").lower() != "queued":
            TaskManager._clear_gpu_wait_info(task)
            return

        task["queued_at"] = info.get("queued_at", task.get("queued_at"))
        incoming = info.get("gpu_wait")
        if not isinstance(incoming, dict):
            task.pop("gpu_wait", None)
            task.pop("_gpu_wait_persisted_signature", None)
            return
        current = task.get("gpu_wait")
        if isinstance(current, dict):
            same_wait = (
                current.get("run_index") == incoming.get("run_index")
                and current.get("started_at") == incoming.get("started_at")
            )
            try:
                current_updated = float(current.get("updated_at", 0.0) or 0.0)
                incoming_updated = float(incoming.get("updated_at", 0.0) or 0.0)
            except (TypeError, ValueError):
                current_updated = incoming_updated = 0.0
            if same_wait and current_updated > incoming_updated:
                return
        task["gpu_wait"] = copy.deepcopy(incoming)
        task["_gpu_wait_persisted_signature"] = TaskManager._gpu_wait_semantic_signature(incoming)

    @classmethod
    def _copy_gpu_schedule_info(cls, task: Dict[str, Any], info: Dict[str, Any]) -> None:
        if str(info.get("status", "") or "").lower() != "running":
            task.pop("_scheduled_env", None)
            task.pop("_gpu_assignment", None)
            return

        scheduled_env = info.get("_scheduled_env")
        if isinstance(scheduled_env, dict) and scheduled_env:
            task["_scheduled_env"] = dict(scheduled_env)
        else:
            task.pop("_scheduled_env", None)

        assignment = info.get("_gpu_assignment")
        if isinstance(assignment, dict) and assignment:
            task["_gpu_assignment"] = copy.deepcopy(assignment)
        else:
            task.pop("_gpu_assignment", None)

    @staticmethod
    def _gpu_ids_from_assignment(assignment: Any) -> List[int]:
        if not isinstance(assignment, dict):
            return []
        gpu_ids: List[int] = []
        for raw_id in assignment.get("gpu_ids", []) or []:
            try:
                gpu_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if gpu_id not in gpu_ids:
                gpu_ids.append(gpu_id)
        return gpu_ids

    @staticmethod
    def _next_run_index(task: Dict[str, Any]) -> int:
        run_index = max(1, TaskManager._effective_run_slot_count(task) + 1)
        if run_index > MAX_RUN_HISTORY_SLOTS:
            task_name = str(task.get("name", "") or "task")
            raise ValueError(
                f"Task '{task_name}' reached the run history limit of "
                f"{MAX_RUN_HISTORY_SLOTS}; create a new task to continue."
            )
        return run_index

    @staticmethod
    def _run_slot_has_data(meta: Dict[str, Any], slot: int) -> bool:
        for key in (
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
        ):
            values = list(meta.get(key, []) or [])
            if slot >= len(values):
                continue
            value = values[slot]
            if value not in (None, "", {}, []):
                return True
        return False

    @classmethod
    def _realized_run_slot_count(cls, meta: Dict[str, Any]) -> int:
        total = run_slot_count(meta)
        while total > 0 and not cls._run_slot_has_data(meta, total - 1):
            total -= 1
        return total

    @classmethod
    def _effective_run_slot_count(cls, meta: Dict[str, Any]) -> int:
        if str(meta.get("status", "") or "").lower() == "queued":
            return cls._realized_run_slot_count(meta)
        return run_slot_count(meta)

    @staticmethod
    def _trim_run_slots(meta: Dict[str, Any], total: int) -> None:
        target = max(0, int(total or 0))
        for key in (
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
        ):
            meta[key] = list(meta.get(key, []) or [])[:target]
        meta["run_index"] = target
        meta.pop("_run_index", None)

    @staticmethod
    def _validate_execution_mode(mode: str | None, fallback: str | None = None) -> str:
        """Return a supported execution mode or raise a user-facing error."""

        raw_mode = fallback if mode is None else mode
        normalized = str(raw_mode or "").strip().lower()
        if normalized not in EXECUTION_MODES:
            expected = ", ".join(EXECUTION_MODES)
            raise ValueError(f"Invalid execution_mode {mode!r}; expected one of: {expected}")
        return normalized

    @classmethod
    def _strip_queued_placeholder_run(cls, info: Dict[str, Any]) -> Dict[str, Any]:
        if str(info.get("status", "") or "").lower() != "queued":
            return info
        cleaned = copy.deepcopy(info)
        cls._trim_run_slots(cleaned, cls._realized_run_slot_count(cleaned))
        return cleaned

    def start_batch_tasks(
        self,
        task_ids: List[str],
        execution_mode: str | None = None,
        max_workers: int | None = None,
        expected_run_indices: Dict[str, int] | None = None,
    ) -> List[str]:
        """Queue a batch of tasks for scheduler-driven execution."""

        with self._shutdown_lock:
            if self._shutdown_event.is_set():
                return []
            return self._start_batch_tasks(
                task_ids,
                execution_mode=execution_mode,
                max_workers=max_workers,
                expected_run_indices=expected_run_indices,
            )

    def _start_batch_tasks(
        self,
        task_ids: List[str],
        execution_mode: str | None = None,
        max_workers: int | None = None,
        expected_run_indices: Dict[str, int] | None = None,
    ) -> List[str]:
        """Queue tasks while the lifecycle lock prevents concurrent shutdown."""

        selected_execution_mode = self._validate_execution_mode(execution_mode, self.execution_mode)
        if execution_mode is not None:
            self.execution_mode = selected_execution_mode
        if max_workers is not None:
            self.max_workers = max(1, int(max_workers))

        gpu_config = self._gpu_scheduler_config()
        gpu_enabled = gpu_config.enabled
        to_sync: list[dict[str, Any]] = []
        to_wait_log: list[tuple[Dict[str, Any], int]] = []
        with self._lock:
            available_slots = max(0, int(self.max_workers) - len(self._batch_running_ids))
            for identifier in task_ids:
                task = self._resolve_identifier_locked(identifier)
                if not task:
                    continue
                if task.get("_load_error"):
                    logger.warning("Skip queuing %s: %s", task["name"], task["_load_error"])
                    continue
                if task.get("status") in ("running", "queued"):
                    logger.info("Skip queuing active task %s", task["name"])
                    continue
                expected_status = str(task.get("status", "pending") or "pending")
                run_index = self._next_run_index(task)
                expected_run_index = None
                if expected_run_indices is not None:
                    raw_expected = expected_run_indices.get(str(task["name"]))
                    if type(raw_expected) is not int or raw_expected <= 0:
                        logger.info(
                            "Skip queuing %s: submission has no valid expected run",
                            task["name"],
                        )
                        continue
                    expected_run_index = raw_expected
                    if run_index != expected_run_index:
                        logger.info(
                            "Skip queuing %s: expected run %d, found next run %d",
                            task["name"],
                            expected_run_index,
                            run_index,
                        )
                        continue
                self._clear_gpu_schedule_state(task)
                if gpu_enabled:
                    wait_state = self._new_gpu_wait_state(run_index, gpu_config)
                    to_sync.append({
                        "name": task["name"],
                        "status": "queued",
                        "run_index": run_index,
                        "expected_run_index": expected_run_index,
                        "expected_status": expected_status,
                        "gpu_wait": wait_state,
                        "queued_independent": False,
                        "wait_log": True,
                        "submit": False,
                        "counts_for_batch": True,
                    })
                elif available_slots > 0:
                    to_sync.append({
                        "name": task["name"],
                        "status": "running",
                        "run_index": run_index,
                        "expected_run_index": expected_run_index,
                        "expected_status": expected_status,
                        "submit": True,
                        "counts_for_batch": True,
                    })
                    available_slots -= 1
                else:
                    to_sync.append({
                        "name": task["name"],
                        "status": "queued",
                        "run_index": run_index,
                        "expected_run_index": expected_run_index,
                        "expected_status": expected_status,
                        "submit": False,
                        "counts_for_batch": True,
                    })
            self._recompute_processing_flag_locked()

        logger.info(
            "Prepared %d task(s) for execution (%d immediate, %d queued)",
            len(to_sync),
            sum(1 for item in to_sync if item.get("submit")),
            sum(1 for item in to_sync if not item.get("submit")),
        )
        to_submit: list[tuple[Dict[str, Any], int]] = []
        claimed_names: list[str] = []
        for item in to_sync:
            task_name = str(item["name"])
            synced = self._sync_status_to_disk(
                task_name,
                str(item["status"]),
                run_index=int(item["run_index"]),
                expected_statuses={str(item["expected_status"])},
                expected_run_index=item.get("expected_run_index"),
                counts_for_batch=bool(item.get("counts_for_batch", True)),
                gpu_wait=item.get("gpu_wait"),
            )
            if not synced:
                continue
            claimed_names.append(task_name)
            with self._lock:
                current = self._resolve_identifier_locked(task_name)
                if not current:
                    continue
                if item.get("gpu_wait") is not None:
                    current["_queued_independent"] = bool(item.get("queued_independent"))
                if item.get("wait_log"):
                    to_wait_log.append((current, int(item["run_index"])))
                if item.get("submit") and current.get("status") == "running":
                    to_submit.append((current, int(item["run_index"])))
        for task, run_index in to_wait_log:
            self._append_gpu_wait_started(task, run_index, gpu_config)
        self.trigger_update()
        for task, run_index in to_submit:
            self._submit_task(task, run_index, independent=False)
        return claimed_names

    def start_task_now(
        self,
        task_id: str,
        execution_mode: str | None = None,
    ) -> bool:
        """Immediately submit a single task outside the batch queue."""

        with self._shutdown_lock:
            if self._shutdown_event.is_set():
                return False
            return self._start_task_now(task_id, execution_mode)

    def _start_task_now(
        self,
        task_id: str,
        execution_mode: str | None = None,
    ) -> bool:
        """Start one task while the lifecycle lock prevents concurrent shutdown."""
        # Independent runs should not silently inherit the most recent batch mode.
        # Callers can still request process mode explicitly.
        execution_mode = self._validate_execution_mode(execution_mode, "thread")

        target = None
        run_index = 1
        target_name = ""
        expected_status = ""
        wait_state: Dict[str, Any] | None = None
        gpu_config = self._gpu_scheduler_config()
        with self._lock:
            target = self._resolve_identifier_locked(task_id)
            if target:
                if target.get("status") in ("running", "queued"):
                    logger.info("Skip starting active task %s", target["name"])
                    return False
                if target.get("_load_error"):
                    logger.warning("Skip running %s: %s", target["name"], target["_load_error"])
                    return False
                target_name = str(target["name"])
                expected_status = str(target.get("status", "pending") or "pending")
                run_index = self._next_run_index(target)
                self._clear_gpu_schedule_state(target)
                if gpu_config.enabled:
                    wait_state = self._new_gpu_wait_state(run_index, gpu_config)
                self._recompute_processing_flag_locked()

        if not target:
            return False

        if gpu_config.enabled:
            synced = self._sync_status_to_disk(
                target_name,
                "queued",
                run_index=run_index,
                expected_statuses={expected_status},
                counts_for_batch=False,
                gpu_wait=wait_state,
            )
            if synced:
                with self._lock:
                    current = self._resolve_identifier_locked(target_name)
                    if current:
                        current["_queued_independent"] = True
                        current["_queued_execution_mode"] = execution_mode
                        target = current
                self._append_gpu_wait_started(target, run_index, gpu_config)
                self.trigger_update()
            return synced

        synced = self._sync_status_to_disk(
            target_name,
            "running",
            run_index=run_index,
            expected_statuses={expected_status},
            counts_for_batch=False,
        )
        if synced:
            with self._lock:
                current = self._resolve_identifier_locked(target_name)
                if current:
                    target = current
            self.trigger_update()
            self._submit_task(target, run_index, independent=True, execution_mode=execution_mode)
        return synced

    def rerun_task(self, task_id: str) -> bool:
        """Queue a completed, failed, or cancelled task again."""

        with self._shutdown_lock:
            if self._shutdown_event.is_set():
                return False
            return self._rerun_task(task_id)

    def _rerun_task(self, task_id: str) -> bool:
        """Queue one rerun while the lifecycle lock prevents concurrent shutdown."""
        gpu_config = self._gpu_scheduler_config()
        target_name = ""
        expected_status = ""
        wait_state: Dict[str, Any] | None = None
        with self._lock:
            target = self._resolve_identifier_locked(task_id)
            if not target or target["status"] not in ("completed", "failed", "cancelled"):
                return False
            if target.get("_load_error"):
                logger.warning("Skip re-queuing %s: %s", target["name"], target["_load_error"])
                return False

            target_name = str(target["name"])
            expected_status = str(target.get("status", "pending") or "pending")
            run_index = self._next_run_index(target)
            self._clear_gpu_schedule_state(target)
            if gpu_config.enabled:
                wait_state = self._new_gpu_wait_state(run_index, gpu_config)
            self._recompute_processing_flag_locked()

        synced = self._sync_status_to_disk(
            target_name,
            "queued",
            run_index=run_index,
            expected_statuses={expected_status},
            counts_for_batch=True,
            gpu_wait=wait_state,
        )
        if not synced:
            self.trigger_update()
            return False
        if gpu_config.enabled:
            with self._lock:
                current = self._resolve_identifier_locked(target_name)
                if current:
                    current["_queued_independent"] = False
                    target = current
            self._append_gpu_wait_started(target, run_index, gpu_config)
        self.trigger_update()
        return True

    def set_task_pinned(self, task_name: str, pinned: Optional[bool] = None) -> tuple[bool, bool | str]:
        """Toggle or set a task's pinned state and sync in-memory caches."""
        with self._lock:
            target = self._resolve_identifier_locked(task_name)
            if not target:
                return False, "Task not found"
            new_value = (not bool(target.get("pinned", False))) if pinned is None else bool(pinned)
            task_dir = target["dir"]

        def _apply(task_info: Dict[str, Any]) -> None:
            task_info["pinned"] = new_value

        updated = update_task_info(task_dir, _apply)
        with self._lock:
            current = self._resolve_identifier_locked(task_name)
            if current:
                self._apply_info_to_task(current, updated)
        self.trigger_update()
        return True, new_value

    def reorder_tasks(self, items: List[Dict[str, Any]]) -> tuple[bool, List[Dict[str, Any]] | str]:
        """Persist manual task order and optional pinned states."""
        normalized: list[tuple[str, Optional[bool], int]] = []
        seen: set[str] = set()
        for order, item in enumerate(items):
            task_name = str((item or {}).get("name", ""))
            if not task_name:
                continue
            if task_name in seen:
                return False, f"Duplicate task in reorder request: {task_name}"
            seen.add(task_name)
            pinned_value = (item or {}).get("pinned")
            normalized.append(
                (
                    task_name,
                    None if pinned_value is None else bool(pinned_value),
                    order,
                )
            )

        if not normalized:
            return False, "No valid tasks were provided for reordering."

        with self._lock:
            updates: list[tuple[str, str, Optional[bool], int]] = []
            for task_name, pinned_value, order in normalized:
                target = self._resolve_identifier_locked(task_name)
                if not target:
                    return False, f"Task not found: {task_name}"
                updates.append((task_name, target["dir"], pinned_value, order))

        updated_info: dict[str, Dict[str, Any]] = {}
        for task_name, task_dir, pinned_value, order in updates:
            def _apply(
                task_info: Dict[str, Any],
                pinned_value: Optional[bool] = pinned_value,
                order: int = order,
            ) -> None:
                task_info["task_order"] = order
                if pinned_value is not None:
                    task_info["pinned"] = pinned_value

            updated_info[task_name] = update_task_info(task_dir, _apply)

        with self._lock:
            for task_name, info in updated_info.items():
                current = self._resolve_identifier_locked(task_name)
                if current:
                    self._apply_info_to_task(current, info)
            reordered = [
                self.serialize_task(self._resolve_identifier_locked(task_name))
                for task_name, _, _ in normalized
            ]

        self.trigger_update()
        return True, [task for task in reordered if task is not None]

    def update_task_notes(
        self,
        task_name: str,
        notes: str,
        expected_notes: str,
    ) -> tuple[bool, str]:
        """Persist task notes and refresh derived search/preview fields."""
        with self._lock:
            target = self._resolve_identifier_locked(task_name)
            if not target:
                return False, "Task not found"
            task_dir = target["dir"]

        def _apply(task_info: Dict[str, Any]) -> None:
            current_notes = str(task_info.get("notes", "") or "")
            if current_notes != expected_notes:
                raise TaskStateConflict("Task notes changed since they were loaded.")
            task_info["notes"] = str(notes or "")

        updated = update_task_info(task_dir, _apply)
        with self._lock:
            current = self._resolve_identifier_locked(task_name)
            if current:
                self._apply_info_to_task(current, updated)
        self.trigger_update()
        return True, str(updated.get("notes", "") or "")

    def update_task_env(
        self,
        task_name: str,
        env: Dict[str, Any],
        expected_env: Dict[str, Any],
    ) -> tuple[bool, Dict[str, Any] | str]:
        """Persist task env vars and sync in-memory task state."""
        with self._lock:
            target = self._resolve_identifier_locked(task_name)
            if not target:
                return False, "Task not found"
            task_dir = target["dir"]

        try:
            normalized_env = normalize_environment(env)
            normalized_expected_env = normalize_environment(expected_env)
        except ValueError as exc:
            return False, str(exc)

        def _apply(task_info: Dict[str, Any]) -> None:
            current_env = normalize_environment(task_info.get("env", {}))
            if current_env != normalized_expected_env:
                raise TaskStateConflict("Task environment changed since it was loaded.")
            task_info["env"] = normalized_env
            task_info.pop("custom_env", None)

        updated = update_task_info(task_dir, _apply)
        with self._lock:
            current = self._resolve_identifier_locked(task_name)
            if current:
                self._apply_info_to_task(current, updated)
        self.trigger_update()
        return True, dict(updated.get("env", {}) or {})

    def rename_task(self, old_name: str, new_name: str) -> tuple[bool, str]:
        """Rename a task by renaming both the folder and the stored task name."""
        new_name = str(new_name or "")
        if not new_name:
            return False, "Task name cannot be empty"

        with self._lock:
            target = self._resolve_identifier_locked(old_name)
            if not target:
                return False, "Task not found"
            if target["status"] in ("running", "queued"):
                return False, "Running or queued tasks cannot be renamed"
            if new_name == target["name"]:
                return True, target["name"]

            err = validate_task_name(new_name, self.tasks_dir)
            if err:
                return False, err

            old_dir = target["dir"]
            new_dir = os.path.join(self.tasks_dir, new_name)
            if os.path.exists(new_dir):
                return False, f"Task name '{new_name}' already exists in the current workspace"

            try:
                disk_info = load_task_info(old_dir, raise_error=True)
                move_token = self._begin_namespace_operation(
                    old_dir,
                    str(target["name"]),
                    kind="rename",
                    expected_run_index=active_task_run_index(disk_info),
                )
            except Exception as exc:
                return False, str(exc)

            try:
                os.rename(old_dir, new_dir)
            except OSError as exc:
                self._rollback_namespace_operation(old_dir, move_token)
                return False, str(exc)

            try:
                updated = self._finish_namespace_operation(
                    new_dir,
                    move_token,
                    new_name=new_name,
                )
            except Exception as exc:
                try:
                    os.rename(new_dir, old_dir)
                except OSError:
                    pass
                self._rollback_namespace_operation(old_dir, move_token)
                return False, str(exc)

            target["dir"] = new_dir.replace("\\", "/")
            target["name"] = new_name
            self._apply_info_to_task(target, updated)
            self._refresh_derived_fields(target)
            self._rebuild_indexes_locked()

        self.trigger_update()
        event_sys.emit("on_task_rename", old_name, new_name)
        return True, new_name

    def request_task_cancel(
        self,
        task_id: str,
        *,
        expected_runner_id: str | None = None,
        expected_run_index: int | None = None,
    ) -> bool:
        """Request cancellation or safely reconcile work whose runner disappeared."""
        with self._lock:
            target = self._resolve_identifier_locked(task_id)
            if not target:
                return False
            task_name = str(target.get("name", "") or "")
            task_dir = str(target.get("dir", "") or "")
        if not task_name or not task_dir:
            return False

        requested_at = get_now_str()
        request_context: Dict[str, Any] = {
            "action": "",
            "original_status": "",
            "finalized_run_slot": False,
            "run_index": 0,
            "runner_id": "",
        }

        def _request(info: Dict[str, Any]) -> None:
            if expected_runner_id is not None and (
                str(info.get("runner_id", "") or "") != expected_runner_id
            ):
                raise TaskStateConflict("task ownership changed before cancellation")
            status = str(info.get("status", "") or "").lower()
            if status not in {"queued", "running"}:
                raise TaskStateConflict(f"task is not active: {status}")
            current_run_index = active_task_run_index(info)
            if expected_run_index is not None:
                if current_run_index != expected_run_index:
                    raise TaskStateConflict(
                        "task run changed before cancellation"
                    )
            info["cancel_requested_at"] = requested_at
            request_context["original_status"] = status
            request_context["run_index"] = current_run_index
            request_context["runner_id"] = str(info.get("runner_id", "") or "")
            if self._is_current_runner(info):
                request_context["action"] = "cancel_local"
                return
            if self._is_foreign_live_runner(info):
                request_context["action"] = "request_foreign"
                return

            final_status = "cancelled" if status == "queued" else "failed"
            _, finalized_run_slot = self._apply_terminal_status_to_info(
                info,
                run_index=current_run_index,
                finish_now=requested_at,
                final_status=final_status,
            )
            request_context["action"] = f"reconcile_{status}"
            request_context["finalized_run_slot"] = finalized_run_slot

        try:
            updated = update_task_info(task_dir, _request)
        except (FileNotFoundError, TaskStateConflict):
            return False

        with self._lock:
            current = self._resolve_identifier_locked(task_name)
            if current and self._same_task_dir(current.get("dir"), task_dir):
                self._apply_info_to_task(current, updated)
        self.trigger_update()

        action = request_context["action"]
        if action == "cancel_local":
            return self.cancel_task(
                task_name,
                expected_runner_id=str(request_context["runner_id"] or ""),
                expected_run_index=int(request_context["run_index"] or 0),
            )
        if action.startswith("reconcile_"):
            status = str(request_context["original_status"] or "")
            if request_context["finalized_run_slot"]:
                display_run_index = max(
                    int(request_context["run_index"] or 0),
                    int(updated.get("run_index", 0) or 0),
                    1,
                )
                title = f"Run #{display_run_index} failed at {requested_at}"
            elif status == "queued":
                title = f"Queued task stopped at {requested_at}"
            else:
                title = f"Task failed at {requested_at}"
            self._append_error_summary(
                task_dir,
                title=title,
                detail_lines=[
                    "reason=runner_unavailable_during_cancel",
                    f"previous_status={status}",
                ],
            )
            logger.warning(
                "%s: runner unavailable during cancellation; reconciled %s as %s",
                task_name,
                status,
                updated.get("status"),
            )
        return True

    def _process_cancel_requests(self) -> None:
        """Apply persisted requests only to tasks owned by this runner."""
        with self._lock:
            candidates = [
                (str(task.get("name", "") or ""), str(task.get("dir", "") or ""))
                for task in self.tasks
                if task and str(task.get("status", "") or "") in {"queued", "running"}
            ]
        for task_name, task_dir in candidates:
            if not task_name or not task_dir:
                continue
            info = load_task_info(task_dir) or {}
            if not info.get("cancel_requested_at") or not self._is_current_runner(info):
                continue
            self.cancel_task(
                task_name,
                expected_runner_id=str(info.get("runner_id", "") or ""),
                expected_run_index=active_task_run_index(info),
            )

    def cancel_task(
        self,
        task_id: str,
        *,
        expected_runner_id: str | None = None,
        expected_run_index: int | None = None,
    ) -> bool:
        """Cancel a queued or running task."""
        target_name = ""
        target_ref: Dict[str, Any] | None = None

        with self._lock:
            target = self._resolve_identifier_locked(task_id)
            if not target or target["status"] not in ("queued", "running"):
                return False
            target_name = target["name"]
            target_ref = dict(target)

        if target_ref is None:
            return False

        disk_info = load_task_info(target_ref["dir"])
        if not disk_info:
            return False
        disk_status = str(disk_info.get("status", "") or "").lower()
        if disk_status not in {"queued", "running"}:
            self._refresh_memory_task_from_disk_info(target_name, target_ref["dir"], disk_info)
            self.trigger_update()
            return False
        disk_runner_id = str(disk_info.get("runner_id", "") or "")
        disk_run_index = active_task_run_index(disk_info)
        if (
            expected_runner_id is not None
            and disk_runner_id != expected_runner_id
        ) or (
            expected_run_index is not None
            and disk_run_index != expected_run_index
        ):
            self._refresh_memory_task_from_disk_info(target_name, target_ref["dir"], disk_info)
            self.trigger_update()
            return False
        if (
            (disk_status == "running" and not self._is_current_runner(disk_info))
            or (disk_status == "queued" and self._is_foreign_live_runner(disk_info))
        ):
            self._refresh_memory_task_from_disk_info(target_name, target_ref["dir"], disk_info)
            self.trigger_update()
            return False

        previous_status = disk_status
        was_running = disk_status == "running"
        action_task = dict(target_ref)
        action_task["status"] = disk_status
        action_task["run_index"] = disk_run_index

        if was_running:
            pid, created_at = self._current_process_identity(disk_info)
            try:
                self._persist_pending_stop_summary(
                    action_task,
                    event="stopped",
                    reason="cancelled_by_user",
                    detail_lines=[f"previous_status={previous_status}"],
                    lock_timeout_sec=_STOP_TASK_INFO_LOCK_TIMEOUT_SEC,
                    expected_statuses={"running"},
                    require_current_runner=True,
                    expected_runner_id=disk_runner_id,
                    expected_run_index=disk_run_index,
                )
            except TimeoutError as exc:
                logger.warning("Could not lock task state to cancel %s: %s", target_name, exc)
                return False
            except (TaskClaimConflict, TaskStateConflict) as exc:
                logger.info("Cancel skipped for %s because disk state changed: %s", target_name, exc)
                latest = load_task_info(target_ref["dir"]) or disk_info
                self._refresh_memory_task_from_disk_info(target_name, target_ref["dir"], latest)
                self.trigger_update()
                return False
            if pid:
                if created_at is None:
                    logger.warning(
                        "Refusing to stop %s because PID %s has no recorded creation time",
                        target_name,
                        pid,
                    )
                    self._clear_pending_stop_request(
                        target_ref["dir"],
                        run_index=action_task["run_index"],
                    )
                    return False
                if not self._should_kill_task_process(disk_info or {}):
                    self._clear_pending_stop_request(
                        target_ref["dir"],
                        run_index=action_task["run_index"],
                    )
                    return False
                if not kill_process(int(pid), expected_create_time=created_at):
                    logger.warning(
                        "Could not verify termination of PID %s for %s",
                        pid,
                        target_name,
                    )
                    self._clear_pending_stop_request(
                        target_ref["dir"],
                        run_index=action_task["run_index"],
                    )
                    latest = load_task_info(target_ref["dir"]) or disk_info
                    self._refresh_memory_task_from_disk_info(
                        target_name,
                        target_ref["dir"],
                        latest,
                    )
                    self.trigger_update()
                    return False
        else:
            try:
                self._mark_failed_on_disk(
                    action_task,
                    event="stopped",
                    reason="cancelled_by_user",
                    detail_lines=[f"previous_status={previous_status}"],
                    lock_timeout_sec=_STOP_TASK_INFO_LOCK_TIMEOUT_SEC,
                    expected_statuses={"queued"},
                    expected_runner_id=disk_runner_id,
                    expected_run_index=disk_run_index,
                    final_status="cancelled",
                )
            except TimeoutError as exc:
                logger.warning("Could not lock queued task state to cancel %s: %s", target_name, exc)
                return False
            except (TaskClaimConflict, TaskStateConflict) as exc:
                logger.info("Cancel skipped for %s because disk state changed: %s", target_name, exc)
                latest = load_task_info(target_ref["dir"]) or disk_info
                self._refresh_memory_task_from_disk_info(target_name, target_ref["dir"], latest)
                self.trigger_update()
                return False

        latest = load_task_info(target_ref["dir"]) or action_task
        with self._lock:
            current = self._tasks_by_name.get(target_name)
            if current and self._same_task_dir(str(current.get("dir", "") or ""), target_ref["dir"]):
                self._apply_info_to_task(current, latest)
                if str(latest.get("status", "") or "").lower() not in {"queued", "running"}:
                    self.gpu_scheduler.release(target_name)
            self._recompute_processing_flag_locked()
            logger.info(
                "%s task %s",
                "Cancellation requested for" if was_running else "Cancelled",
                target_name,
            )
        self.trigger_update()
        return True

    def delete_tasks(self, task_ids: List[str]) -> List[str]:
        """Soft-delete tasks by moving folders into .trash."""
        candidates: list[Dict[str, Any]] = []
        seen: set[str] = set()
        with self._lock:
            for identifier in task_ids:
                target = self._resolve_identifier_locked(identifier)
                if not target:
                    continue
                target_name = str(target.get("name", "") or "")
                if not target_name or target_name in seen:
                    continue
                seen.add(target_name)
                candidates.append({
                    "name": target_name,
                    "dir": target["dir"],
                    "status": target.get("status"),
                    "run_index": target.get("run_index", 0),
                })

        if not candidates:
            return []

        # Validate trash before stopping or otherwise mutating any selected task.
        trash_dir = os.path.join(self.tasks_dir, TRASH_DIR)
        validate_tasks_root(self.tasks_dir)
        validate_tasks_root(trash_dir)
        os.makedirs(trash_dir, exist_ok=True)
        validate_tasks_root(trash_dir)

        targets: list[Dict[str, Any]] = []
        for candidate in candidates:
            disk_info = load_task_info(candidate["dir"])
            disk_status = str((disk_info or {}).get("status", candidate.get("status", "")) or "").lower()
            if (
                (disk_status == "running" and not self._is_current_runner(disk_info or {}))
                or (disk_status == "queued" and self._is_foreign_live_runner(disk_info or {}))
            ):
                logger.info("Delete skipped for %s because another runner owns it", candidate["name"])
                if disk_info:
                    self._refresh_memory_task_from_disk_info(candidate["name"], candidate["dir"], disk_info)
                continue

            action_task = dict(candidate)
            if disk_info:
                action_task["run_index"] = active_task_run_index(disk_info)
            action_task["status"] = disk_status

            if disk_status in {"queued", "running"}:
                previous_status = disk_status
                if disk_status == "running":
                    pid, created_at = self._current_process_identity(disk_info or {})
                    try:
                        self._persist_pending_stop_summary(
                            action_task,
                            event="stopped",
                            reason="deleted_while_active",
                            detail_lines=[f"previous_status={previous_status}"],
                            lock_timeout_sec=_STOP_TASK_INFO_LOCK_TIMEOUT_SEC,
                            expected_statuses={"running"},
                            require_current_runner=True,
                            expected_runner_id=str((disk_info or {}).get("runner_id", "") or ""),
                            expected_run_index=active_task_run_index(disk_info or {}),
                        )
                    except TimeoutError as exc:
                        logger.warning(
                            "Delete skipped for %s because task state is busy: %s",
                            candidate["name"],
                            exc,
                        )
                        continue
                    except (TaskClaimConflict, TaskStateConflict) as exc:
                        logger.info(
                            "Delete skipped for %s because disk state changed: %s",
                            candidate["name"],
                            exc,
                        )
                        latest = load_task_info(candidate["dir"])
                        if latest:
                            self._refresh_memory_task_from_disk_info(
                                candidate["name"],
                                candidate["dir"],
                                latest,
                            )
                        continue

                    if pid:
                        terminated = bool(
                            created_at is not None
                            and self._should_kill_task_process(disk_info or {})
                            and kill_process(int(pid), expected_create_time=created_at)
                        )
                        if not terminated:
                            self._clear_pending_stop_request(
                                candidate["dir"],
                                run_index=action_task["run_index"],
                            )
                            logger.warning(
                                "Delete skipped for %s because process termination was not verified",
                                candidate["name"],
                            )
                            continue

                    settled = self._wait_for_task_settle(
                        candidate["name"],
                        candidate["dir"],
                    )
                    if settled is None:
                        logger.warning(
                            "Delete skipped for %s because its worker did not settle",
                            candidate["name"],
                        )
                        continue
                    with self._lock:
                        current = self._tasks_by_name.get(candidate["name"])
                        if current and self._same_task_dir(str(current.get("dir", "") or ""), candidate["dir"]):
                            self._apply_info_to_task(current, settled)
                            self._clear_running_locked(candidate["name"])
                            self.gpu_scheduler.release(candidate["name"])
                            self._recompute_processing_flag_locked()
                else:
                    try:
                        self._mark_failed_on_disk(
                            action_task,
                            event="stopped",
                            reason="deleted_while_active",
                            detail_lines=[f"previous_status={previous_status}"],
                            lock_timeout_sec=_STOP_TASK_INFO_LOCK_TIMEOUT_SEC,
                            expected_statuses={"queued"},
                            expected_runner_id=str((disk_info or {}).get("runner_id", "") or ""),
                            expected_run_index=active_task_run_index(disk_info or {}),
                        )
                    except TimeoutError as exc:
                        logger.warning(
                            "Delete skipped for %s because task state is busy: %s",
                            candidate["name"],
                            exc,
                        )
                        continue
                    except (TaskClaimConflict, TaskStateConflict) as exc:
                        logger.info(
                            "Delete skipped for %s because disk state changed: %s",
                            candidate["name"],
                            exc,
                        )
                        latest = load_task_info(candidate["dir"])
                        if latest:
                            self._refresh_memory_task_from_disk_info(
                                candidate["name"],
                                candidate["dir"],
                                latest,
                            )
                        continue
                    with self._lock:
                        current = self._tasks_by_name.get(candidate["name"])
                        if current and self._same_task_dir(
                            str(current.get("dir", "") or ""),
                            candidate["dir"],
                        ):
                            current["status"] = "failed"
                            self.gpu_scheduler.release(candidate["name"])
                            self._recompute_processing_flag_locked()

            try:
                validate_task_directory(candidate["dir"])
                source_identity = self._directory_identity(candidate["dir"])
            except ValueError as exc:
                logger.warning("Delete skipped for %s: %s", candidate["name"], exc)
                continue
            if source_identity is None or not os.path.isdir(candidate["dir"]):
                logger.warning(
                    "Delete skipped for %s because its task directory disappeared",
                    candidate["name"],
                )
                continue

            targets.append({
                "name": candidate["name"],
                "dir": candidate["dir"],
                "identity": source_identity,
                "run_index": active_task_run_index(
                    settled if disk_status == "running" else (load_task_info(candidate["dir"]) or action_task)
                ),
            })

        self.trigger_update()

        deleted_names: list[str] = []
        for target in targets:
            folder = os.path.basename(target["dir"])
            moved = False
            destination = ""
            move_token = ""
            try:
                move_token = self._begin_namespace_operation(
                    target["dir"],
                    str(target["name"]),
                    kind="delete",
                    expected_run_index=int(target["run_index"]),
                )
                # One shared namespace lock coordinates delete with restore.  The
                # destination stays on the same filesystem and os.rename never
                # interprets an existing directory as a container.
                with task_info_lock(trash_dir):
                    validate_tasks_root(self.tasks_dir)
                    validate_tasks_root(trash_dir)
                    validate_task_directory(target["dir"])
                    if self._directory_identity(target["dir"]) != target["identity"]:
                        raise OSError("task directory identity changed before delete")

                    for attempt in range(3):
                        destination = os.path.join(trash_dir, folder)
                        if os.path.lexists(destination):
                            destination = os.path.join(
                                trash_dir,
                                f"{folder}_{get_now_str()}_{uuid.uuid4().hex[:8]}",
                            )
                        if os.path.lexists(destination):
                            continue
                        try:
                            os.rename(target["dir"], destination)
                            moved = True
                            break
                        except FileExistsError:
                            continue
                        except PermissionError:
                            if attempt >= 2:
                                raise
                            time.sleep(0.2)
                    if moved:
                        try:
                            self._finish_namespace_operation(destination, move_token)
                        except Exception as exc:
                            logger.warning(
                                "Moved %s to trash but could not clear its move marker: %s",
                                target["name"],
                                exc,
                            )
            except Exception as exc:
                logger.error("Error moving task to trash safely: %s", exc)
            if not moved and move_token:
                self._rollback_namespace_operation(target["dir"], move_token)
            if moved:
                deleted_names.append(str(target["name"]))

        if deleted_names:
            deleted_set = set(deleted_names)
            with self._lock:
                self.tasks = [
                    task
                    for task in self.tasks
                    if str((task or {}).get("name", "") or "") not in deleted_set
                ]
                self._clear_running_many_locked(deleted_set)
                self._rebuild_indexes_locked()
                self._recompute_processing_flag_locked()
            self.trigger_update()

        return deleted_names

    def _scheduler_loop(self) -> None:
        """Submit queued tasks up to max_workers and keep UI state fresh."""
        last_trigger = 0.0
        last_refresh = 0.0
        last_reactive_refresh = 0.0
        while not self._shutdown_event.is_set():
            try:
                now = time.time()
                reactive_refresh_due = (
                    self.has_reactive_watchers()
                    and now - last_reactive_refresh >= _REACTIVE_DISK_REFRESH_INTERVAL_SEC
                )
                should_refresh = (
                    self._running_ids or
                    self.is_processing or
                    reactive_refresh_due or
                    (now - last_refresh >= 1.0)
                )

                if should_refresh:
                    last_refresh = now
                    if reactive_refresh_due:
                        last_reactive_refresh = now
                    if self.refresh_from_disk(
                        check_all=reactive_refresh_due,
                        discover=reactive_refresh_due,
                    ):
                        if now - last_trigger >= 1.0:
                            last_trigger = now
                            self.trigger_update()
                    self._refresh_queued_runner_leases()
                    self._process_cancel_requests()

                if not self.is_processing:
                    if self._shutdown_event.wait(0.5):
                        break
                    continue

                self._ensure_executor()

                independent_only = False
                if len(self._batch_running_ids) >= self.max_workers:
                    with self._lock:
                        has_independent_queued = any(
                            task
                            and task.get("status") == "queued"
                            and bool(task.get("_queued_independent"))
                            for task in self.tasks
                        )
                    if not has_independent_queued:
                        if self._shutdown_event.wait(0.1):
                            break
                        continue
                    independent_only = True

                target, run_index = self._pick_queued_task(independent_only=independent_only)
                if not target:
                    with self._lock:
                        self._recompute_processing_flag_locked()
                    if self._shutdown_event.wait(0.1):
                        break
                    continue

                independent = bool(target.pop("_queued_independent", False))
                queued_mode = target.pop("_queued_execution_mode", None)
                self._submit_task(
                    target,
                    run_index,
                    independent=independent,
                    execution_mode=queued_mode,
                )
            except Exception as exc:
                logger.error("Scheduler error: %s", exc, exc_info=True)
                if self._shutdown_event.wait(1):
                    break

            if self._shutdown_event.wait(0.2):
                break

    def _ensure_executor(self) -> None:
        """Create or recreate the batch executor when mode/worker count changes."""
        with self._executor_lock:
            if self._shutdown_event.is_set():
                raise RuntimeError("task manager is shutting down")
            self.execution_mode = self._validate_execution_mode(self.execution_mode, "thread")
            workers = max(1, int(self.max_workers))
            changed = self._executor_mode != self.execution_mode or self._executor_workers != workers
            if self._executor and not changed:
                return
            if self._executor:
                try:
                    self._executor.shutdown(wait=False)
                except Exception:
                    pass

            cls = ProcessPoolExecutor if self.execution_mode == "process" else ThreadPoolExecutor
            self._executor = cls(max_workers=workers)
            self._executor_mode = self.execution_mode
            self._executor_workers = workers

    @staticmethod
    def _queue_started_at_value(task: Dict[str, Any]) -> float:
        values = [task.get("queued_at")]
        gpu_wait = task.get("gpu_wait")
        if isinstance(gpu_wait, dict):
            values.append(gpu_wait.get("started_at"))
        for raw in values:
            try:
                value = float(raw or 0.0)
            except (TypeError, ValueError):
                continue
            if value > 0 and value == value:
                return value
        return -1.0

    def _queued_candidates_locked(
        self,
        *,
        independent_only: bool,
    ) -> List[Dict[str, Any]]:
        candidates = [
            (index, task)
            for index, task in enumerate(self.tasks)
            if (
                task
                and task.get("status") == "queued"
                and not self._is_foreign_live_runner(task)
                and (not independent_only or task.get("_queued_independent"))
            )
        ]
        ordered = sorted(
            candidates,
            key=lambda item: (self._queue_started_at_value(item[1]), -item[0]),
        )
        return [task for _index, task in ordered]

    def _queued_candidate_locked(self, *, independent_only: bool) -> Dict[str, Any] | None:
        candidates = self._queued_candidates_locked(independent_only=independent_only)
        return candidates[0] if candidates else None

    def _pick_queued_task(self, *, independent_only: bool = False) -> tuple[Dict[str, Any] | None, int]:
        """Pick the next queued task and mark it running."""
        gpu_config = self._gpu_scheduler_config()
        if not gpu_config.enabled:
            with self._lock:
                task = self._queued_candidate_locked(independent_only=independent_only)
                if task:
                    run_index = self._next_run_index(task)
                    is_independent = bool(task.get("_queued_independent"))
                    task["status"] = "running"
                    task["run_index"] = run_index
                    self._mark_running_locked(task["name"], counts_for_batch=not is_independent)
                    self._recompute_processing_flag_locked()
                    return task, run_index
                self._recompute_processing_flag_locked()
            return None, 1

        return self._pick_queued_gpu_task(gpu_config, independent_only=independent_only)

    def _pick_queued_gpu_task(
        self,
        gpu_config: GpuSchedulerConfig,
        *,
        independent_only: bool,
    ) -> tuple[Dict[str, Any] | None, int]:
        """Evaluate one GPU queue pass under a single cross-process schedule lock."""

        monotonic_now = self.gpu_scheduler.clock()
        wall_now = time.time()
        wait_logs: List[tuple[Dict[str, Any], List[str]]] = []
        assignment_log: tuple[Dict[str, Any], GpuAssignment] | None = None
        timeout_log: tuple[Dict[str, Any], int, float, float] | None = None
        gpu_wait_persists: List[tuple[str, str, Dict[str, Any], tuple[Any, ...]]] = []
        target: Dict[str, Any] | None = None
        result_run_index = 1
        workspace_env: Dict[str, str] | None = None

        # GPU sampling may invoke nvidia-smi; keep it outside the cross-process
        # admission lock so other schedulers are not blocked on device I/O.
        self.gpu_scheduler.snapshot(gpu_config, now=monotonic_now)
        try:
            with task_info_lock(self.tasks_dir, timeout_sec=_GPU_SCHEDULE_LOCK_TIMEOUT_SEC):
                self._sync_gpu_reservations_from_running_tasks()
                with self._lock:
                    candidate_names = [
                        str(task.get("name", "") or "")
                        for task in self._queued_candidates_locked(independent_only=independent_only)
                    ]
                for task_name in candidate_names:
                    with self._lock:
                        candidate = self._resolve_identifier_locked(task_name)
                        if (
                            not candidate
                            or candidate.get("status") != "queued"
                            or self._is_foreign_live_runner(candidate)
                            or (independent_only and not candidate.get("_queued_independent"))
                        ):
                            continue
                        is_independent = bool(candidate.get("_queued_independent"))
                        run_index = self._next_run_index(candidate)
                        wait_state = self._ensure_gpu_wait_state(
                            candidate,
                            run_index,
                            gpu_config,
                            now=wall_now,
                        )
                        started_at = float(wait_state["started_at"])
                        deadline_at = float(wait_state["deadline_at"])
                        waited = max(0.0, wall_now - started_at)
                        max_wait = max(0.0, deadline_at - started_at)
                        # Match executor._prepare_env so admission checks see
                        # the same visibility mask as the eventual worker.
                        if workspace_env is None:
                            workspace_env = _load_workspace_global_env(candidate.get("dir"))
                        task_env = dict(workspace_env)
                        task_env.update(candidate.get("env", {}) or {})

                    if wall_now >= deadline_at:
                        self.gpu_scheduler.release(task_name)
                        try:
                            self._mark_failed_on_disk(
                                candidate,
                                event="failed",
                                reason="gpu_wait_timeout",
                                detail_lines=[
                                    f"waited={self._format_duration(waited)}",
                                    f"max_wait={self._format_duration(max_wait)}",
                                ],
                                expected_statuses={"queued"},
                            )
                        except (TaskClaimConflict, TaskStateConflict) as exc:
                            logger.info(
                                "GPU wait timeout skipped for %s because disk state changed: %s",
                                task_name,
                                exc,
                            )
                            latest = load_task_info(candidate["dir"])
                            if latest:
                                self._refresh_memory_task_from_disk_info(task_name, candidate["dir"], latest)
                        else:
                            timeout_log = (candidate, run_index, waited, max_wait)
                        break

                    decision = self.gpu_scheduler.try_reserve(
                        task_name,
                        run_index,
                        gpu_config,
                        task_env=task_env,
                        queued_since=monotonic_now - waited,
                        refresh_snapshot=False,
                    )

                    with self._lock:
                        current = self._resolve_identifier_locked(task_name)
                        if not current or current.get("status") != "queued":
                            if decision.assignment is not None:
                                self.gpu_scheduler.release(task_name)
                            continue
                        run_index = self._next_run_index(current)
                        wait_changed = self._update_gpu_wait_state(
                            current,
                            run_index,
                            gpu_config,
                            decision,
                            waited=waited,
                            now=wall_now,
                        )

                        if decision.assignment is None:
                            if wait_changed:
                                wait_snapshot = copy.deepcopy(current.get("gpu_wait"))
                                wait_signature = self._gpu_wait_semantic_signature(wait_snapshot)
                                if isinstance(wait_snapshot, dict) and wait_signature is not None:
                                    gpu_wait_persists.append(
                                        (
                                            str(current.get("name", "") or ""),
                                            str(current.get("dir", "") or ""),
                                            wait_snapshot,
                                            wait_signature,
                                        )
                                    )
                            lines = self._gpu_wait_decision_lines(
                                current,
                                run_index,
                                gpu_config,
                                decision,
                                waited,
                                monotonic_now,
                            )
                            if lines:
                                wait_logs.append((current, lines))
                            self._recompute_processing_flag_locked()
                        else:
                            assignment = dataclasses.replace(decision.assignment, run_index=run_index)
                            current["_scheduled_env"] = dict(assignment.env)
                            current["_gpu_assignment"] = self._gpu_assignment_to_dict(assignment)
                            current["status"] = "running"
                            current["run_index"] = run_index
                            self._mark_running_locked(current["name"], counts_for_batch=not is_independent)
                            self._recompute_processing_flag_locked()
                            assignment_log = (current, assignment)
                            target = current
                            result_run_index = run_index

                    if target is not None and self._claim_task_for_run(
                        target,
                        run_index,
                        counts_for_batch=not is_independent,
                    ) is None:
                        self.gpu_scheduler.release(task_name)
                        with self._lock:
                            current = self._resolve_identifier_locked(task_name)
                            if current:
                                info = load_task_info(current["dir"])
                                if info:
                                    info = self._strip_queued_placeholder_run(info)
                                    self._apply_info_to_task(current, info)
                                self._clear_running_locked(task_name)
                                self._clear_gpu_schedule_state(current)
                                self._recompute_processing_flag_locked()
                        target = None
                        assignment_log = None
                        # A claim race can reveal a newly running foreign task.
                        # Refresh reservations before evaluating another candidate.
                        self._sync_gpu_reservations_from_running_tasks()
                        continue
                    if target is not None:
                        break
                with self._lock:
                    self._recompute_processing_flag_locked()
        except TimeoutError as exc:
            logger.debug("GPU scheduler lock busy: %s", exc)
            return None, 1

        for task_name, task_dir, wait_snapshot, wait_signature in gpu_wait_persists:
            self._persist_gpu_wait_semantics(
                task_name,
                task_dir,
                wait_snapshot,
                wait_signature,
            )

        for wait_task, lines in wait_logs:
            self._append_gpu_wait_refresh(wait_task, lines)
        if wait_logs:
            self.trigger_update()
        if timeout_log:
            timeout_task, timeout_run_index, waited, max_wait = timeout_log
            self._append_gpu_queue_log(
                timeout_task,
                "GPU WAIT TIMEOUT",
                [
                    f"Run #{timeout_run_index} GPU wait timed out after {self._format_duration(waited)}",
                    f"max wait={self._format_duration(max_wait)}",
                ],
            )
            self.trigger_update()
        if assignment_log:
            self._append_gpu_assignment(assignment_log[0], assignment_log[1])
            return target, result_run_index
        return None, 1

    def _submit_task(
        self,
        target: Dict[str, Any],
        run_index: int,
        *,
        independent: bool,
        execution_mode: str | None = None,
    ) -> None:
        """Persist a running state and submit one task to the chosen executor."""

        with self._shutdown_lock:
            if self._shutdown_event.is_set():
                return
            self._submit_task_before_shutdown(
                target,
                run_index,
                independent=independent,
                execution_mode=execution_mode,
            )

    def _submit_task_before_shutdown(
        self,
        target: Dict[str, Any],
        run_index: int,
        *,
        independent: bool,
        execution_mode: str | None = None,
    ) -> None:
        """Submit one task while the lifecycle lock is held."""

        if self._claim_task_for_run(target, run_index, counts_for_batch=not independent) is None:
            self.gpu_scheduler.release(target["name"])
            with self._lock:
                self._clear_running_locked(target["name"])
                current = self._resolve_identifier_locked(target["name"])
                if current:
                    info = load_task_info(current["dir"])
                    if info:
                        info = self._strip_queued_placeholder_run(info)
                        self._apply_info_to_task(current, info)
                self._recompute_processing_flag_locked()
            self.trigger_update()
            return

        try:
            if independent:
                mode = self._validate_execution_mode(execution_mode, self.execution_mode)
                with self._executor_lock:
                    if self._independent_executor and self._independent_executor_mode != mode:
                        try:
                            self._independent_executor.shutdown(wait=False)
                        except Exception:
                            pass
                        self._independent_executor = None
                    if not self._independent_executor:
                        cls = ProcessPoolExecutor if mode == "process" else ThreadPoolExecutor
                        self._independent_executor = cls(max_workers=32)
                        self._independent_executor_mode = mode
                executor = self._independent_executor
            else:
                self._ensure_executor()
                executor = self._executor

            assert executor is not None
            task_env = {str(k): str(v) for k, v in (target.get("env", {}) or {}).items()}
            task_env.update({str(k): str(v) for k, v in (target.get("_scheduled_env", {}) or {}).items()})
            future = executor.submit(
                run_task_worker,
                target["dir"],
                target["name"],
                target["created_at"],
                target["config"],
                task_env,
                run_index,
                self.runner_id,
                self.runner_host,
                self.lease_seconds,
            )
            future.add_done_callback(lambda fut, tid=target["name"]: self._on_task_done(fut, tid))
            logger.debug(
                "Submitted task %s to %s executor (batch_running=%d/%d, local_running=%d)",
                target["name"],
                "independent" if independent else "batch",
                len(self._batch_running_ids),
                self.max_workers,
                len(self._running_ids),
            )
        except Exception as exc:
            self.gpu_scheduler.release(target["name"])
            with self._lock:
                self._clear_running_locked(target["name"])
                self._clear_gpu_schedule_state(target)
                target["status"] = "failed"
                self._recompute_processing_flag_locked()
            self._mark_failed_on_disk(
                target,
                reason="submission_error",
                detail_lines=[
                    f"exception={type(exc).__name__}: {exc}",
                    f"independent={independent}",
                ],
            )
            logger.error("Failed to submit task %s: %s", target["name"], exc)

    def _on_task_done(self, future: Future, task_id: str) -> None:
        """Handle worker completion and pull final state from disk."""
        self.gpu_scheduler.release(task_id)
        worker_error = None
        try:
            exc = future.exception()
            if exc:
                worker_error = exc
                logger.error("Worker for %s raised: %s", task_id, exc)
        except Exception:
            pass

        need_mark_failed = False
        task_ref = None
        with self._lock:
            self._clear_running_locked(task_id)
            task = self._tasks_by_name.get(task_id)
            if not task:
                self._recompute_processing_flag_locked()
                self.trigger_update()
                return

            try:
                info = load_task_info(task["dir"])
                if info:
                    info = self._strip_queued_placeholder_run(info)
                    self._apply_info_to_task(task, info)
            except Exception:
                pass

            self._clear_gpu_schedule_state(task)
            if worker_error and task["status"] in ("running", "queued"):
                task["status"] = "failed"
                need_mark_failed = True
                task_ref = task

            self._recompute_processing_flag_locked()

        # Disk I/O outside the lock to avoid potential deadlock
        if need_mark_failed and task_ref:
            self._mark_failed_on_disk(
                task_ref,
                reason="worker_exception",
                detail_lines=[f"exception={type(worker_error).__name__}: {worker_error}"],
            )

        self.trigger_update()

    def _cleanup_on_shutdown(self) -> None:
        """Fail any queued/running tasks when the app is shutting down."""
        with self._shutdown_lock:
            if self._shutdown_cleanup_done:
                return
            self._shutdown_cleanup_done = True

        logger.info("System shutting down; cleaning up stuck task states...")
        acquired = self._lock.acquire(timeout=2.0)
        if not acquired:
            with self._shutdown_lock:
                self._shutdown_cleanup_done = False
            logger.warning("Skip shutdown cleanup: task manager lock not acquired in time.")
            return

        try:
            active_tasks = [
                task
                for task in self.tasks
                if task and task.get("status") in ("running", "queued")
            ]
        finally:
            self._lock.release()

        changed = False
        for task in active_tasks:
            status = task.get("status")
            task_name = str(task.get("name", ""))
            disk_info = load_task_info(task["dir"])
            if not disk_info and not os.path.isdir(task["dir"]):
                if status == "running":
                    pid, created_at = self._current_process_identity(task)
                    if pid and created_at is not None and self._should_kill_task_process(task):
                        try:
                            pid_value = int(pid)
                            if pid_value != os.getpid():
                                logger.info(
                                    "Shutdown cleanup: terminating running process %s for deleted task %s",
                                    pid_value,
                                    task_name,
                                )
                                kill_process(
                                    pid_value,
                                    expected_create_time=created_at,
                                )
                        except Exception as exc:
                            logger.warning("Failed to kill pid %s on shutdown cleanup: %s", pid, exc)
                with self._lock:
                    current = self._resolve_identifier_locked(task_name)
                    if current:
                        current["status"] = "failed"
                        self._clear_running_locked(task_name)
                        self.gpu_scheduler.release(task_name)
                        changed = True
                continue
            disk_status = str((disk_info or {}).get("status", status) or "").lower()
            if disk_info and disk_status not in {"queued", "running"}:
                continue
            if disk_info and self._is_foreign_live_runner(disk_info):
                continue
            expected_runner_id = str((disk_info or {}).get("runner_id", "") or "")
            expected_run_index = active_task_run_index(disk_info or {})
            termination_verified = True
            if disk_status == "running":
                pid, created_at = self._current_process_identity(disk_info or {})
                if pid and self._should_kill_task_process(disk_info or {}):
                    try:
                        logger.info(
                            "Shutdown cleanup: terminating running process %s for task %s",
                            pid,
                            task_name,
                        )
                        termination_verified = bool(
                            created_at is not None
                            and kill_process(
                                int(pid),
                                expected_create_time=created_at,
                            )
                        )
                    except Exception as exc:
                        termination_verified = False
                        logger.warning("Failed to kill pid %s on shutdown cleanup: %s", pid, exc)
            if not termination_verified:
                logger.warning(
                    "Shutdown cleanup left %s running because process termination was not verified",
                    task_name,
                )
                continue

            try:
                self._mark_failed_on_disk(
                    task,
                    event="stopped",
                    reason="system_shutdown",
                    detail_lines=["detail=Task forcibly terminated due to system shutdown or Ctrl+C."],
                    lock_timeout_sec=_STOP_TASK_INFO_LOCK_TIMEOUT_SEC,
                    expected_statuses={disk_status},
                    expected_runner_id=expected_runner_id,
                    expected_run_index=expected_run_index,
                )
            except (TaskClaimConflict, TaskStateConflict, TimeoutError) as exc:
                logger.warning("Could not persist shutdown state for %s yet: %s", task_name, exc)
                continue

            with self._lock:
                current = self._resolve_identifier_locked(task_name)
                if current:
                    current["status"] = "failed"
                    self._clear_running_locked(task_name)
                    self.gpu_scheduler.release(task_name)
                    changed = True

        with self._lock:
            self._recompute_processing_flag_locked()

        if changed:
            self.trigger_update()

    def shutdown(self) -> None:
        """Stop background scheduling and release executors promptly."""
        with self._shutdown_lock:
            if self._atexit_registered:
                try:
                    atexit.unregister(self._atexit_callback)
                except ValueError:
                    pass
                self._atexit_registered = False
            self._shutdown_event.set()
        if self.owns_task_lifecycle:
            self._cleanup_on_shutdown()

        with self._executor_lock:
            executors = [self._executor, self._independent_executor]
            self._executor = None
            self._independent_executor = None
            self._independent_executor_mode = None
            self._executor_mode = None
            self._executor_workers = 0

        for executor in executors:
            if executor is None:
                continue
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                executor.shutdown(wait=False)
            except Exception as exc:
                logger.debug("Executor shutdown failed: %s", exc)

    def _resolve_identifier_locked(self, identifier: str | None) -> Dict[str, Any] | None:
        if not identifier:
            return None
        return self._tasks_by_name.get(identifier)

    @staticmethod
    def _latest_pid(info: Dict[str, Any]) -> Any:
        pids = info.get("pids", [])
        if not isinstance(pids, list):
            return None
        for pid in reversed(pids):
            if pid:
                return pid
        return None

    @staticmethod
    def _current_process_identity(
        info: Dict[str, Any],
    ) -> tuple[int | None, float | None]:
        """Return the PID identity for the current run slot, never an older run."""

        try:
            run_index = int(info.get("run_index", 0) or 0)
        except (TypeError, ValueError):
            return None, None
        if run_index <= 0:
            return None, None

        pids = list(info.get("pids", []) or [])
        if run_index > len(pids):
            return None, None
        try:
            pid = int(pids[run_index - 1])
        except (TypeError, ValueError):
            return None, None
        if pid <= 0:
            return None, None

        create_times = list(info.get("pid_create_times", []) or [])
        if run_index > len(create_times):
            return pid, None
        try:
            created_at = float(create_times[run_index - 1])
        except (TypeError, ValueError):
            return pid, None
        if created_at <= 0 or created_at != created_at:
            return pid, None
        return pid, created_at

    def _latest_pid_from_disk(self, task: Dict[str, Any]) -> Any:
        task_info = load_task_info(task["dir"])
        return self._latest_pid(task_info) if task_info else None

    def _set_runner_lease_fields(self, info: Dict[str, Any]) -> None:
        now = time.time()
        info["runner_id"] = self.runner_id
        info["runner_host"] = self.runner_host
        info["lease_heartbeat"] = now
        info["lease_until"] = now + max(1, int(self.lease_seconds))

    def _refresh_queued_runner_leases(self) -> None:
        """Keep locally queued tasks owned while they wait for workers or GPUs."""

        now = time.time()
        interval = min(DEFAULT_RUNNER_HEARTBEAT_SECONDS, max(1.0, self.lease_seconds / 3))
        if now - self._last_queued_lease_heartbeat < interval:
            return
        self._last_queued_lease_heartbeat = now

        with self._lock:
            queued = [
                (
                    str(task.get("name", "") or ""),
                    str(task.get("dir", "") or ""),
                    copy.deepcopy(task.get("gpu_wait")) if isinstance(task.get("gpu_wait"), dict) else None,
                )
                for task in self.tasks
                if task and task.get("status") == "queued" and not self._is_foreign_live_runner(task)
            ]

        for task_name, task_dir, gpu_wait in queued:
            if not task_name or not task_dir:
                continue

            def _apply(info: Dict[str, Any]) -> None:
                if str(info.get("status", "") or "").lower() != "queued":
                    raise TaskStateConflict("task is no longer queued")
                if self._is_foreign_live_runner(info):
                    raise TaskClaimConflict("queued task already owned by another runner")
                self._set_runner_lease_fields(info)
                if isinstance(gpu_wait, dict):
                    info["gpu_wait"] = copy.deepcopy(gpu_wait)

            try:
                updated = update_task_info(task_dir, _apply)
            except (FileNotFoundError, TaskClaimConflict, TaskStateConflict):
                continue
            with self._lock:
                current = self._resolve_identifier_locked(task_name)
                if current and self._same_task_dir(current.get("dir"), task_dir):
                    self._apply_info_to_task(current, updated)

    @staticmethod
    def _clear_runner_lease_fields(info: Dict[str, Any]) -> None:
        for key in ("runner_id", "runner_host", "lease_heartbeat", "lease_until"):
            info.pop(key, None)

    def _claim_task_for_run(
        self,
        task: Dict[str, Any],
        run_index: int,
        *,
        counts_for_batch: bool,
    ) -> Dict[str, Any] | None:
        """Atomically claim one task before submitting it to a local worker."""
        task_name = str(task.get("name", "") or "")
        task_dir = str(task.get("dir", "") or "")
        if not task_name or not task_dir:
            return None
        with self._lock:
            current = self._resolve_identifier_locked(task_name)
            if not current or not self._same_task_dir(current.get("dir"), task_dir):
                return None

        info_path = os.path.join(task_dir, TASK_INFO_FILENAME)
        if not os.path.isfile(info_path):
            return None

        def _apply(info: Dict[str, Any]) -> None:
            if info.get("_creation_rollback"):
                raise TaskStateConflict("task is being rolled back after failed creation")
            self._guard_namespace_operation(info)
            status = str(info.get("status", "pending") or "pending").lower()
            if status == "running":
                if not self._is_current_runner(info):
                    raise TaskClaimConflict("task already owned by another runner")
            elif status == "queued":
                if self._is_foreign_live_runner(info):
                    raise TaskClaimConflict("queued task already owned by another runner")
            else:
                raise TaskStateConflict(f"task is no longer claimable: {status}")
            info["status"] = "running"
            info["run_index"] = run_index
            self._clear_gpu_wait_info(info)
            scheduled_env = task.get("_scheduled_env")
            if isinstance(scheduled_env, dict) and scheduled_env:
                info["_scheduled_env"] = {str(k): str(v) for k, v in scheduled_env.items() if str(k)}
            else:
                info.pop("_scheduled_env", None)
            assignment = task.get("_gpu_assignment")
            if isinstance(assignment, dict) and assignment:
                info["_gpu_assignment"] = copy.deepcopy(assignment)
            else:
                info.pop("_gpu_assignment", None)
            self._set_runner_lease_fields(info)

        try:
            updated = update_task_info(task_dir, _apply)
        except (FileNotFoundError, TaskClaimConflict, TaskStateConflict) as exc:
            logger.info("Skip submitting %s: %s", task_name, exc)
            return None

        with self._lock:
            current = self._resolve_identifier_locked(task_name)
            if current and self._same_task_dir(current.get("dir"), task_dir):
                self._apply_info_to_task(current, updated)
                current["status"] = "running"
                self._mark_running_locked(current["name"], counts_for_batch=counts_for_batch)
                self._recompute_processing_flag_locked()
            else:
                return None
        return updated

    def _sync_status_to_disk(
        self,
        identifier: str,
        status: str,
        run_index: int = 1,
        *,
        expected_statuses: set[str] | None = None,
        expected_run_index: int | None = None,
        counts_for_batch: bool = True,
        gpu_wait: Dict[str, Any] | None = None,
    ) -> bool:
        """Persist transient queue/running status changes."""
        with self._lock:
            task = self._resolve_identifier_locked(identifier)
            if not task:
                return False
            task_dir = task["dir"]
        info_path = os.path.join(task_dir, TASK_INFO_FILENAME)
        if not os.path.isfile(info_path):
            return False
        expected = {str(item).lower() for item in (expected_statuses or set())}
        next_status = str(status or "").lower()

        def _apply(task_info: Dict[str, Any]) -> None:
            if task_info.get("_creation_rollback"):
                raise TaskStateConflict("task is being rolled back after failed creation")
            self._guard_namespace_operation(task_info)
            current_status = str(task_info.get("status", "pending") or "pending").lower()
            if expected and current_status not in expected:
                raise TaskStateConflict(
                    f"expected {sorted(expected)}, found {current_status}"
                )
            actual_next_run = self._next_run_index(task_info)
            if expected_run_index is not None and (
                type(expected_run_index) is not int
                or expected_run_index <= 0
                or run_index != expected_run_index
            ):
                raise TaskStateConflict("invalid expected task run")
            if next_status in {"queued", "running"} and actual_next_run != run_index:
                raise TaskStateConflict(
                    f"expected next run {run_index}, found {actual_next_run}"
                )
            if next_status in {"queued", "running"} and current_status == "running" and self._is_foreign_live_runner(task_info):
                raise TaskClaimConflict("task already owned by another live runner")
            task_info["status"] = next_status
            if next_status == "running":
                task_info["run_index"] = run_index
            task_info.pop("_queued_run_index", None)
            if next_status == "queued":
                self._trim_run_slots(task_info, self._realized_run_slot_count(task_info))
                task_info.pop("cancel_requested_at", None)
                started_at = (
                    float(gpu_wait.get("started_at", 0.0) or 0.0)
                    if isinstance(gpu_wait, dict)
                    else time.time()
                )
                task_info["queued_at"] = started_at if started_at > 0 else time.time()
                if isinstance(gpu_wait, dict):
                    task_info["gpu_wait"] = copy.deepcopy(gpu_wait)
                else:
                    task_info.pop("gpu_wait", None)
            else:
                self._clear_gpu_wait_info(task_info)
            if next_status in {"queued", "running"}:
                self._set_runner_lease_fields(task_info)
            else:
                self._clear_runner_lease_fields(task_info)
            if next_status != "running":
                self._clear_gpu_schedule_info(task_info)

        try:
            updated = update_task_info(task_dir, _apply)
        except (FileNotFoundError, TaskClaimConflict, TaskStateConflict) as exc:
            logger.info("Skip syncing %s as %s: %s", identifier, status, exc)
            try:
                refreshed = load_task_info(task_dir)
            except Exception:
                refreshed = None
            if refreshed:
                refreshed = self._strip_queued_placeholder_run(refreshed)
            with self._lock:
                current = self._resolve_identifier_locked(identifier)
                if current and refreshed:
                    self._apply_info_to_task(current, refreshed)
                self._clear_running_locked(identifier)
                self.gpu_scheduler.release(identifier)
                self._clear_gpu_schedule_state(current or {})
                self._recompute_processing_flag_locked()
            self.trigger_update()
            return False

        with self._lock:
            current = self._resolve_identifier_locked(identifier)
            if current and updated and self._same_task_dir(current.get("dir"), task_dir):
                self._apply_info_to_task(current, updated)
                if current["status"] == "running":
                    self._mark_running_locked(identifier, counts_for_batch=counts_for_batch)
                elif current.get("status") != "running":
                    self._clear_running_locked(identifier)
                self._recompute_processing_flag_locked()
        return True

    def _settings_root(self) -> str:
        """Return the workspace root used for shared settings."""

        return os.path.dirname(os.path.abspath(self.tasks_dir))

    def _gpu_scheduler_config(self) -> GpuSchedulerConfig:
        """Load the current workspace GPU scheduler settings."""

        return GpuSchedulerConfig.from_settings(load_settings(self._settings_root()))

    @staticmethod
    def _new_gpu_wait_state(
        run_index: int,
        config: GpuSchedulerConfig,
        *,
        started_at: float | None = None,
    ) -> Dict[str, Any]:
        start = float(time.time() if started_at is None else started_at)
        max_wait = max(1.0, float(config.max_wait_seconds or 1.0))
        return {
            "state": "waiting",
            "run_index": int(run_index),
            "started_at": start,
            "deadline_at": start + max_wait,
            "waited_seconds": 0.0,
            "remaining_seconds": max_wait,
            "max_wait_seconds": max_wait,
            "requested_gpu_count": config.required_gpu_count,
            "eligible_gpu_count": 0,
            "total_gpu_count": 0,
            "reason": "Waiting for GPU scheduler",
            "devices": [],
            "updated_at": start,
        }

    @staticmethod
    def _gpu_wait_reason_signature(reason: Any) -> str:
        normalized = str(reason or "")
        for pattern, replacement in _GPU_WAIT_REASON_NORMALIZERS:
            normalized = pattern.sub(replacement, normalized)
        return normalized

    @staticmethod
    def _gpu_wait_semantic_signature(wait: Dict[str, Any] | None) -> tuple[Any, ...] | None:
        """Return the persisted GPU-wait meaning, excluding elapsed clock fields."""

        if not isinstance(wait, dict):
            return None
        devices = wait.get("devices")
        device_signature = tuple(
            (
                device.get("index"),
                device.get("name"),
                device.get("uuid"),
                device.get("eligible"),
                TaskManager._gpu_wait_reason_signature(device.get("reason")),
            )
            for device in (devices if isinstance(devices, list) else [])
            if isinstance(device, dict)
        )
        return (
            wait.get("state"),
            wait.get("run_index"),
            wait.get("started_at"),
            wait.get("deadline_at"),
            wait.get("max_wait_seconds"),
            wait.get("requested_gpu_count"),
            wait.get("eligible_gpu_count"),
            wait.get("total_gpu_count"),
            TaskManager._gpu_wait_reason_signature(wait.get("reason")),
            device_signature,
        )

    def _persist_gpu_wait_semantics(
        self,
        task_name: str,
        task_dir: str,
        wait: Dict[str, Any],
        signature: tuple[Any, ...],
    ) -> bool:
        """Persist a changed GPU-wait decision once without extending its lease."""

        if not task_name or not task_dir:
            return False
        wait_run_index = wait.get("run_index")
        wait_started_at = wait.get("started_at")

        def _apply(info: Dict[str, Any]) -> None:
            if str(info.get("status", "") or "").lower() != "queued":
                raise TaskStateConflict("task is no longer queued")
            if self._is_foreign_live_runner(info):
                raise TaskClaimConflict("queued task already owned by another runner")
            persisted_wait = info.get("gpu_wait")
            if isinstance(persisted_wait, dict) and (
                persisted_wait.get("run_index") != wait_run_index
                or persisted_wait.get("started_at") != wait_started_at
            ):
                raise TaskStateConflict("GPU wait generation changed")
            info["gpu_wait"] = copy.deepcopy(wait)

        try:
            update_task_info(task_dir, _apply)
        except (FileNotFoundError, OSError, TimeoutError, TaskClaimConflict, TaskStateConflict) as exc:
            logger.debug("Could not persist GPU wait details for %s yet: %s", task_name, exc)
            return False

        with self._lock:
            current = self._resolve_identifier_locked(task_name)
            if (
                current
                and self._same_task_dir(current.get("dir"), task_dir)
                and current.get("status") == "queued"
                and self._gpu_wait_semantic_signature(current.get("gpu_wait")) == signature
            ):
                current["_gpu_wait_persisted_signature"] = signature
        return True

    def _ensure_gpu_wait_state(
        self,
        task: Dict[str, Any],
        run_index: int,
        config: GpuSchedulerConfig,
        *,
        now: float,
    ) -> Dict[str, Any]:
        legacy_started = task.get("_gpu_wait_started_at")
        legacy_wall_started = 0.0
        try:
            legacy_elapsed = self.gpu_scheduler.clock() - float(legacy_started)
        except (TypeError, ValueError):
            legacy_elapsed = -1.0
        if legacy_elapsed >= 0:
            legacy_wall_started = max(0.0, now - legacy_elapsed)

        current = task.get("gpu_wait")
        try:
            current_run_index = int(current.get("run_index", 0) or 0) if isinstance(current, dict) else 0
        except (TypeError, ValueError):
            current_run_index = 0
        if isinstance(current, dict) and current_run_index == int(run_index):
            try:
                started_at = float(current.get("started_at", 0.0) or 0.0)
                deadline_at = float(current.get("deadline_at", 0.0) or 0.0)
            except (TypeError, ValueError):
                started_at = deadline_at = 0.0
            if started_at > 0:
                try:
                    max_wait = max(1.0, float(current.get("max_wait_seconds", config.max_wait_seconds) or 1.0))
                except (TypeError, ValueError):
                    max_wait = max(1.0, float(config.max_wait_seconds or 1.0))
                if legacy_wall_started > 0 and legacy_wall_started < started_at:
                    started_at = legacy_wall_started
                    current["started_at"] = started_at
                    current["deadline_at"] = started_at + max_wait
                    deadline_at = float(current["deadline_at"])
                if deadline_at <= 0:
                    deadline_at = started_at + max_wait
                    current["deadline_at"] = deadline_at
                task["queued_at"] = started_at
                return current

        try:
            queued_at = float(task.get("queued_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            queued_at = 0.0
        state = self._new_gpu_wait_state(
            run_index,
            config,
            started_at=(legacy_wall_started or queued_at or now),
        )
        task["gpu_wait"] = state
        task["queued_at"] = state["started_at"]
        return state

    @staticmethod
    def _update_gpu_wait_state(
        task: Dict[str, Any],
        run_index: int,
        config: GpuSchedulerConfig,
        decision: GpuDecision,
        *,
        waited: float,
        now: float,
    ) -> bool:
        state = task.get("gpu_wait")
        if not isinstance(state, dict):
            state = TaskManager._new_gpu_wait_state(run_index, config, started_at=now - waited)
        try:
            deadline_at = float(state.get("deadline_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            deadline_at = now + max(0.0, float(config.max_wait_seconds or 0.0) - waited)
        state.update(
            {
                "state": "waiting",
                "run_index": int(run_index),
                "waited_seconds": max(0.0, float(waited)),
                "remaining_seconds": max(0.0, deadline_at - now),
                "requested_gpu_count": config.required_gpu_count,
                "eligible_gpu_count": int(decision.eligible_gpu_count),
                "total_gpu_count": int(decision.total_gpu_count),
                "reason": str(decision.reason or "waiting"),
                "devices": [
                    {
                        "index": device.index,
                        "name": device.name,
                        "uuid": device.uuid,
                        "eligible": device.eligible,
                        "reason": device.reason,
                        "memory_used_pct": device.memory_used_pct,
                        "free_memory_gb": device.free_memory_gb,
                        "compute_util_pct": device.compute_util_pct,
                    }
                    for device in decision.devices
                ],
                "updated_at": now,
            }
        )
        task["gpu_wait"] = state
        return (
            TaskManager._gpu_wait_semantic_signature(state)
            != task.get("_gpu_wait_persisted_signature")
        )

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format queue wait durations for compact log messages."""

        total = max(0, int(round(float(seconds or 0))))
        if total >= 3600:
            hours = total / 3600
            if total % 3600 == 0:
                return f"{int(hours)}h"
            return f"{hours:.1f}h"
        if total >= 60:
            minutes = total / 60
            if total % 60 == 0:
                return f"{int(minutes)}m"
            return f"{minutes:.1f}m"
        return f"{total}s"

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total = max(0, int(round(float(seconds or 0))))
        hours = total // 3600
        minutes = (total % 3600) // 60
        secs = total % 60
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    @staticmethod
    def _gpu_need_label(config: GpuSchedulerConfig) -> str:
        count = config.required_gpu_count
        return f"{count} GPU{'s' if count != 1 else ''}"

    @staticmethod
    def _gpu_pool_label(config: GpuSchedulerConfig) -> str:
        if not config.device_ids:
            return "not set" if config.uses_specified_devices else "any"
        label = ",".join(str(device_id) for device_id in config.device_ids)
        return f"specified {label}" if config.uses_specified_devices else label

    @staticmethod
    def _gpu_assignment_to_dict(assignment: GpuAssignment) -> Dict[str, Any]:
        return {
            "task_name": assignment.task_name,
            "run_index": assignment.run_index,
            "gpu_ids": list(assignment.gpu_ids),
            "cuda_visible_devices": assignment.cuda_visible_devices,
            "env": dict(assignment.env),
            "waited_seconds": assignment.waited_seconds,
        }

    def _sync_gpu_reservations_from_running_tasks(self) -> None:
        with self._lock:
            known_tasks = {
                str(task.get("name", "") or ""): {
                    "dir": str(task.get("dir", "") or ""),
                    "status": str(task.get("status", "") or "").lower(),
                    "mtime_ns": int(task.get("_mtime_ns", 0) or 0),
                    "foreign_live": self._is_foreign_live_runner(task),
                    "local_live": self._is_current_runner(task) and self._lease_active(task),
                }
                for task in self.tasks
                if task and str(task.get("name", "") or "")
            }

        scan_ok, disk_names = self._scan_task_dir_names()
        if scan_ok:
            task_refs: List[tuple[str, str]] = []
            for task_name in disk_names:
                task_dir = os.path.join(self.tasks_dir, task_name)
                known = known_tasks.get(task_name)
                if known is None or known["status"] == "running" or bool(known["foreign_live"]):
                    task_refs.append((task_name, task_dir))
                    continue
                if known["status"] == "queued" and bool(known["local_live"]):
                    # A locally leased queued task cannot be claimed by another
                    # scheduler, so rereading it here only duplicates refresh I/O.
                    continue
                if known["status"] == "queued":
                    task_refs.append((task_name, task_dir))
                    continue
                try:
                    disk_mtime_ns = os.stat(os.path.join(task_dir, TASK_INFO_FILENAME)).st_mtime_ns
                except OSError:
                    continue
                if disk_mtime_ns != int(known["mtime_ns"] or 0):
                    task_refs.append((task_name, task_dir))
        else:
            task_refs = [
                (task_name, str(task.get("dir", "") or ""))
                for task_name, task in known_tasks.items()
                if task["status"] == "running"
            ]

        reservations: Dict[str, List[int]] = {}
        for task_name, task_dir in task_refs:
            if not task_name or not task_dir:
                continue
            try:
                info = load_task_info(task_dir)
            except Exception:
                continue
            if str(info.get("status", "") or "").lower() != "running":
                continue
            if not (self._is_current_runner(info) or self._is_foreign_live_runner(info)):
                continue
            gpu_ids = self._gpu_ids_from_assignment(info.get("_gpu_assignment"))
            if gpu_ids:
                reservations[task_name] = gpu_ids

        self.gpu_scheduler.sync_reservations(reservations)

    @staticmethod
    def _gpu_queue_run_index(lines: List[str]) -> int | None:
        for line in lines:
            match = _GPU_QUEUE_RUN_RE.search(str(line))
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def _last_gpu_queue_run_index(queue_log: str) -> int | None:
        try:
            with open(queue_log, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 64 * 1024))
                text = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return None

        matches = _GPU_QUEUE_RUN_RE.findall(text)
        return int(matches[-1]) if matches else None

    @staticmethod
    def _gpu_queue_run_separator(run_index: int) -> str:
        return f"[PYRUNS] -------------------- RUN #{run_index} --------------------\n"

    @staticmethod
    def _gpu_queue_paragraph_prefix(last_byte: bytes) -> str:
        return "\n" if last_byte == b"\n" else "\n\n"

    def _append_gpu_queue_log(self, task: Dict[str, Any], title: str, lines: List[str]) -> None:
        updated_at = get_now_str()
        status_summary = str(lines[0] if lines else title).strip()
        run_index = self._gpu_queue_run_index(lines)
        run_context = [f"Run log: run{run_index}.log"] if run_index is not None else []
        detail_lines = [status_summary, f"Updated at {updated_at}", *run_context, *lines[1:]]
        try:
            queue_log = prepare_task_log_path(str(task.get("dir", "")), QUEUE_LOG_FILENAME)
            prefix = ""
            last_run_index = None
            if os.path.exists(queue_log) and os.path.getsize(queue_log) > 0:
                with open(queue_log, "rb") as existing:
                    existing.seek(-1, os.SEEK_END)
                    last_byte = existing.read(1)
                prefix = self._gpu_queue_paragraph_prefix(last_byte)
                last_run_index = task.get("_gpu_queue_log_last_run_index") or self._last_gpu_queue_run_index(queue_log)
            if run_index is not None and last_run_index is not None and run_index != last_run_index:
                prefix = f"{prefix}{self._gpu_queue_run_separator(run_index)}"
            with open(queue_log, "a", encoding="utf-8", newline="") as handle:
                handle.write(prefix)
                handle.write(format_gpu_queue_block(title, detail_lines))
            if run_index is not None:
                task["_gpu_queue_log_last_run_index"] = run_index
        except Exception as exc:
            logger.error("Failed to write GPU queue log for %s: %s", task.get("name"), exc)

    def _append_gpu_wait_refresh(self, task: Dict[str, Any], lines: List[str]) -> None:
        run_index = self._gpu_queue_run_index(lines)
        status_line = self._gpu_wait_refresh_line(lines)
        if not status_line:
            return

        try:
            queue_log = prepare_task_log_path(str(task.get("dir", "")), QUEUE_LOG_FILENAME)
            prefix = ""
            last_run_index = None
            if os.path.exists(queue_log) and os.path.getsize(queue_log) > 0:
                with open(queue_log, "rb") as existing:
                    existing.seek(-1, os.SEEK_END)
                    last_byte = existing.read(1)
                last_run_index = task.get("_gpu_queue_log_last_run_index") or self._last_gpu_queue_run_index(queue_log)
                if run_index is not None and last_run_index is not None and run_index != last_run_index:
                    prefix = f"{self._gpu_queue_paragraph_prefix(last_byte)}{self._gpu_queue_run_separator(run_index)}"
                elif last_byte == b"\n":
                    prefix = "\n"

            previous_width = int(task.get("_gpu_wait_refresh_width", 0) or 0)
            visible_width = len(status_line)
            task["_gpu_wait_refresh_width"] = max(previous_width, visible_width)
            padding = " " * max(0, previous_width - visible_width)
            with open(queue_log, "a", encoding="utf-8", newline="") as handle:
                handle.write(prefix)
                handle.write(f"\r{status_line}{padding}")
            if run_index is not None:
                task["_gpu_queue_log_last_run_index"] = run_index
        except Exception as exc:
            logger.error("Failed to write GPU wait refresh for %s: %s", task.get("name"), exc)

    @staticmethod
    def _gpu_wait_refresh_line(lines: List[str]) -> str:
        clean_lines = [str(line).strip() for line in lines if str(line).strip()]
        if not clean_lines:
            return ""

        summary = clean_lines[0]
        reason = ""
        snapshot = ""
        for line in clean_lines[1:]:
            if line.startswith("Blocked: "):
                reason = "blocked: " + line[len("Blocked: "):]
            elif line.startswith("Stabilizing: "):
                reason = "stabilizing: " + line[len("Stabilizing: "):]
            elif line.startswith("GPU "):
                snapshot = line
                break

        parts = [summary]
        if reason:
            parts.append(reason)
        if snapshot:
            parts.append(snapshot)
        return "[PYRUNS] " + " | ".join(parts)

    def _append_gpu_wait_started(
        self,
        task: Dict[str, Any],
        run_index: int,
        config: GpuSchedulerConfig,
    ) -> None:
        task["_gpu_wait_logged_for"] = run_index
        task["_gpu_last_wait_log_at"] = time.monotonic()
        timeout = self._format_duration(config.max_wait_seconds)
        mode = "multi" if config.required_gpu_count > 1 else "single"
        self._append_gpu_queue_log(
            task,
            "GPU WAIT",
            [
                (
                    f"Run #{run_index} waiting for GPU resources, mode={mode}, "
                    f"need={self._gpu_need_label(config)}, timeout={timeout}, max wait={timeout}"
                ),
                f"GPU selection={config.selection_mode}",
                f"GPU pool={self._gpu_pool_label(config)}",
                format_gpu_rule(config),
                f"Per-GPU concurrency limit={config.max_tasks_per_gpu}",
            ],
        )

    def _append_gpu_wait_decision(
        self,
        task: Dict[str, Any],
        run_index: int,
        config: GpuSchedulerConfig,
        decision: GpuDecision,
        waited: float,
        now: float,
    ) -> None:
        lines = self._gpu_wait_decision_lines(task, run_index, config, decision, waited, now)
        if lines:
            self._append_gpu_wait_refresh(task, lines)

    @staticmethod
    def _gpu_wait_log_interval(config: GpuSchedulerConfig) -> float:
        return max(1.0, float(config.stable_seconds or 1.0))

    def _gpu_wait_decision_lines(
        self,
        task: Dict[str, Any],
        run_index: int,
        config: GpuSchedulerConfig,
        decision: GpuDecision,
        waited: float,
        now: float,
    ) -> List[str] | None:
        reason = str(decision.reason or "waiting")
        last_log_at = float(task.get("_gpu_last_wait_log_at", 0.0) or 0.0)
        periodic_seconds = self._gpu_wait_log_interval(config)
        if last_log_at > now:
            last_log_at = 0.0
        if last_log_at > 0 and now - last_log_at < periodic_seconds:
            return None

        task["_gpu_last_wait_log_at"] = now
        reason_line = "Stabilizing" if "stabilizing" in reason else "Blocked"
        lines = [
            f"Run #{run_index} still waiting after {self._format_elapsed(waited)}",
            f"{reason_line}: {reason}",
            format_gpu_rule(config),
        ]
        lines.extend(self._gpu_snapshot_lines(decision.snapshot, config))
        return lines

    def _append_gpu_assignment(self, task: Dict[str, Any], assignment: GpuAssignment) -> None:
        gpu_label = assignment.cuda_visible_devices or ",".join(str(gpu_id) for gpu_id in assignment.gpu_ids)
        cuda_visible = assignment.cuda_visible_devices or str(assignment.env.get("CUDA_VISIBLE_DEVICES", "") or gpu_label)
        self._append_gpu_queue_log(
            task,
            "GPU ASSIGNED",
            [
                (
                    f"Run #{assignment.run_index} assigned GPUs {gpu_label} "
                    f"after {self._format_elapsed(assignment.waited_seconds)}"
                ),
                f"PYRUNS_ASSIGNED_GPUS={gpu_label}",
                f"CUDA_VISIBLE_DEVICES={cuda_visible}",
            ],
        )

    @staticmethod
    def _gpu_snapshot_lines(snapshot: List[Any], config: GpuSchedulerConfig) -> List[str]:
        if not snapshot:
            return ["GPU snapshot: no NVIDIA GPU metrics available"]

        allowed = set(config.device_ids or [])
        lines: List[str] = []
        for gpu in sorted(snapshot, key=lambda item: int(getattr(item, "index", 0)))[:8]:
            index = int(getattr(gpu, "index", 0))
            if allowed and index not in allowed:
                continue
            memory_pct = float(getattr(gpu, "memory_used_pct", 100.0))
            compute_pct = float(getattr(gpu, "compute_util_pct", 0.0))
            free_gib = float(getattr(gpu, "free_memory_gb", 0.0))
            is_eligible = (
                memory_pct <= config.memory_used_pct
                and compute_pct <= config.compute_used_pct
                and free_gib >= config.min_free_memory_gb
            )
            state = "eligible" if is_eligible else "blocked"
            lines.append(
                f"GPU {index} {state}: memory {memory_pct:.0f}%, "
                f"compute {compute_pct:.0f}%, free {free_gib:.1f} GiB"
            )
        return lines or ["GPU snapshot: configured GPU pool is empty"]

    def _append_error_summary(
        self,
        task_dir: str,
        *,
        title: str,
        detail_lines: List[str],
    ) -> None:
        """Append a structured failure/cancel summary block into error.log."""

        block = (
            f"\n\n{'=' * 70}\n"
            f"[PYRUNS] {title}\n"
            + "\n".join(detail_lines)
            + f"\n{'=' * 70}\n"
        )
        try:
            error_log = prepare_task_log_path(task_dir, ERROR_LOG_FILENAME)
            with open(error_log, "a", encoding="utf-8") as handle:
                handle.write(block)
        except Exception as exc:
            logger.error("Failed to write error.log for %s: %s", task_dir, exc)

    def _clear_pending_stop_request(
        self,
        task_dir: str,
        *,
        run_index: int,
    ) -> bool:
        """Roll back a stop marker after identity verification or termination fails."""

        def _apply(info: Dict[str, Any]) -> None:
            if str(info.get("status", "") or "").lower() != "running":
                return
            if not self._is_current_runner(info):
                return
            summary = info.get("_pending_stop_summary")
            if isinstance(summary, dict):
                try:
                    summary_run = int(summary.get("run_index", 0) or 0)
                except (TypeError, ValueError):
                    summary_run = 0
                if summary_run == int(run_index):
                    info.pop("_pending_stop_summary", None)
            info.pop("cancel_requested_at", None)

        try:
            update_task_info(
                task_dir,
                _apply,
                timeout_sec=_STOP_TASK_INFO_LOCK_TIMEOUT_SEC,
            )
            return True
        except (OSError, TimeoutError, TypeError, ValueError):
            return False

    def _wait_for_task_settle(
        self,
        task_name: str,
        task_dir: str,
        *,
        timeout: float = _ACTIVE_DELETE_SETTLE_TIMEOUT_SEC,
    ) -> Dict[str, Any] | None:
        """Wait until disk is terminal and the local worker released the task."""

        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            try:
                info = load_task_info(task_dir) or {}
            except (OSError, TypeError, ValueError):
                return None
            status = str(info.get("status", "") or "").lower()
            with self._lock:
                locally_running = task_name in self._running_ids
            if status not in {"queued", "running"} and not locally_running:
                return info
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)

    def _persist_pending_stop_summary(
        self,
        task: Dict[str, Any],
        *,
        event: str,
        reason: str,
        detail_lines: List[str] | None = None,
        lock_timeout_sec: float | None = None,
        expected_statuses: set[str] | None = None,
        require_current_runner: bool = False,
        expected_runner_id: str | None = None,
        expected_run_index: int | None = None,
    ) -> None:
        """Store a stop request without publishing a terminal state prematurely."""

        task_dir = task["dir"]
        finish_now = get_now_str()
        run_index = int(task.get("run_index", 0) or 0)

        def _apply(task_info: Dict[str, Any]) -> None:
            original_status = str(task_info.get("status", "") or "").lower()
            if expected_statuses is not None and original_status not in expected_statuses:
                raise TaskStateConflict(f"expected {sorted(expected_statuses)}, found {original_status!r}")
            if require_current_runner and not self._is_current_runner(task_info):
                raise TaskClaimConflict("task already owned by another runner")
            if expected_runner_id is not None and (
                str(task_info.get("runner_id", "") or "") != expected_runner_id
            ):
                raise TaskClaimConflict("task ownership changed before stop request")
            if expected_run_index is not None and (
                active_task_run_index(task_info) != expected_run_index
            ):
                raise TaskStateConflict("task run changed before stop request")
            target_index = max(run_index, run_slot_count(task_info), 1)
            task_info.setdefault("cancel_requested_at", finish_now)
            task_info["_pending_stop_summary"] = {
                "run_index": target_index,
                "event": event,
                "reason": reason,
                "detail_lines": list(detail_lines or []),
            }

        update_kwargs = {}
        if lock_timeout_sec is not None:
            update_kwargs["timeout_sec"] = lock_timeout_sec
        updated = update_task_info(task_dir, _apply, **update_kwargs)
        if "status" in task:
            self._apply_info_to_task(task, updated)

    def _apply_terminal_status_to_info(
        self,
        task_info: Dict[str, Any],
        *,
        run_index: int,
        finish_now: str,
        final_status: str,
    ) -> tuple[str, bool]:
        """Finalize an active task-info payload while its file lock is held."""

        original_status = str(task_info.get("status", "") or "").lower()
        slot_count = (
            self._realized_run_slot_count(task_info)
            if original_status == "queued"
            else run_slot_count(task_info)
        )
        if original_status == "queued":
            self._trim_run_slots(task_info, slot_count)
        target_index = max(run_index, slot_count)
        should_finalize_slot = original_status == "running" and target_index > 0
        if should_finalize_slot:
            slot = ensure_run_slot(task_info, target_index)
            if not task_info["finish_times"][slot]:
                task_info["finish_times"][slot] = finish_now
            task_info["run_statuses"][slot] = final_status
        task_info["status"] = final_status
        task_info["progress"] = 0.0
        self._clear_runner_lease_fields(task_info)
        self._clear_gpu_schedule_info(task_info)
        self._clear_gpu_wait_info(task_info)
        return original_status, should_finalize_slot

    def _mark_failed_on_disk(
        self,
        task: Dict[str, Any],
        *,
        event: str = "failed",
        reason: str | None = None,
        detail_lines: List[str] | None = None,
        lock_timeout_sec: float | None = None,
        expected_statuses: set[str] | None = None,
        require_current_runner: bool = False,
        require_no_live_owner: bool = False,
        expected_runner_id: str | None = None,
        expected_run_index: int | None = None,
        final_status: str = "failed",
    ) -> None:
        """Persist a failed state and finalize the active run slot if needed."""
        task_dir = task["dir"]
        finish_now = get_now_str()
        run_index = int(task.get("run_index", 0) or 0)
        failure_context = {"finalized_run_slot": False, "original_status": ""}

        def _apply(task_info: Dict[str, Any]) -> None:
            original_status = str(task_info.get("status", "") or "").lower()
            if expected_statuses is not None and original_status not in expected_statuses:
                raise TaskStateConflict(f"expected {sorted(expected_statuses)}, found {original_status!r}")
            if require_current_runner and not self._is_current_runner(task_info):
                raise TaskClaimConflict("task already owned by another runner")
            if require_no_live_owner and self._running_info_has_live_owner(task_info):
                raise TaskClaimConflict("task is owned by a live runner")
            if expected_runner_id is not None and (
                str(task_info.get("runner_id", "") or "") != expected_runner_id
            ):
                raise TaskClaimConflict("task ownership changed before terminal update")
            if expected_run_index is not None and (
                active_task_run_index(task_info) != expected_run_index
            ):
                raise TaskStateConflict("task run changed before terminal update")
            original_status, should_finalize_slot = self._apply_terminal_status_to_info(
                task_info,
                run_index=run_index,
                finish_now=finish_now,
                final_status=final_status,
            )
            failure_context["original_status"] = original_status
            failure_context["finalized_run_slot"] = should_finalize_slot

        update_kwargs = {}
        if lock_timeout_sec is not None:
            update_kwargs["timeout_sec"] = lock_timeout_sec
        updated = update_task_info(task_dir, _apply, **update_kwargs)
        if reason or detail_lines:
            if failure_context.get("finalized_run_slot"):
                display_run_index = max(run_index, int(updated.get("run_index", 0) or 0), 1)
                title = f"Run #{display_run_index} {event} at {finish_now}"
            elif failure_context.get("original_status") == "queued":
                title = f"Queued task {event} at {finish_now}"
            else:
                title = f"Task {event} at {finish_now}"
            lines: List[str] = []
            if reason:
                lines.append(f"reason={reason}")
            lines.extend(detail_lines or [])
            self._append_error_summary(
                task_dir,
                title=title,
                detail_lines=lines,
            )
        if "status" in task:
            self._apply_info_to_task(task, updated)
            task["status"] = final_status

    @staticmethod
    def _task_snapshot(task: Dict[str, Any]) -> tuple:
        """Compact comparison tuple for change detection."""
        return (
            task.get("name"),
            task.get("status"),
            task.get("progress"),
            tuple(task.get("start_times", [])),
            tuple(task.get("finish_times", [])),
            tuple(task.get("pids", [])),
            tuple(task.get("pid_create_times", [])),
            tuple(task.get("run_statuses", [])),
            tuple(task.get("durations", [])),
            tuple(task.get("exit_codes", [])),
            tuple(task.get("source_states", [])),
            tuple(repr(item) for item in (task.get("records", []) or [])),
            tuple(repr(item) for item in (task.get("tracks", []) or [])),
            task.get("pinned"),
            task.get("task_order"),
            task.get("task_kind"),
            task.get("config_file"),
            task.get("command_mode"),
            repr(task.get("cmd")),
            task.get("workdir"),
            task.get("shell_executable"),
            task.get("shell_kind"),
            task.get("notes", ""),
            task.get("runner_id"),
            task.get("runner_host"),
            task.get("lease_until"),
            task.get("queued_at"),
            repr(task.get("gpu_wait")),
        )

    def _apply_info_to_task(
        self,
        task: Dict[str, Any],
        info: Dict[str, Any],
        *,
        mtime_ns: int | None = None,
    ) -> None:
        """Copy task_info.json fields used by UI and scheduler."""
        task.update(
            {
                "name": os.path.basename(os.path.normpath(task["dir"])),
                "status": info.get("status", task.get("status", "pending")),
                "progress": info.get("progress", task.get("progress", 0.0)),
                "env": info.get("env", task.get("env", {})),
                "pinned": info.get("pinned", task.get("pinned", False)),
                "task_order": info.get("task_order", task.get("task_order")),
                "script": info.get("script", task.get("script")),
                "command_mode": info.get("command_mode", task.get("command_mode")),
                "cmd": info.get("cmd", task.get("cmd")),
                "workdir": info.get("workdir", task.get("workdir")),
                "shell_executable": info.get("shell_executable", task.get("shell_executable")),
                "shell_kind": info.get("shell_kind", task.get("shell_kind")),
                "task_kind": normalize_task_kind(
                    info.get("task_kind", info.get("config_mode", task.get("task_kind", TASK_KIND_CONFIG)))
                ),
                "config_file": resolve_task_config_file(
                    info,
                    normalize_task_kind(
                        info.get("task_kind", info.get("config_mode", task.get("task_kind", TASK_KIND_CONFIG)))
                    ),
                    task["dir"],
                ),
                "start_times": info.get("start_times", []),
                "finish_times": info.get("finish_times", []),
                "pids": info.get("pids", []),
                "pid_create_times": info.get("pid_create_times", []),
                "run_statuses": info.get("run_statuses", []),
                "durations": info.get("durations", []),
                "exit_codes": info.get("exit_codes", []),
                "source_states": info.get("source_states", []),
                "records": info.get("records", []),
                "tracks": info.get("tracks", []),
                "notes": info.get("notes", ""),
                "run_index": int(info.get("run_index", info.get("_run_index", task.get("run_index", 0))) or 0),
                "runner_id": info.get("runner_id"),
                "runner_host": info.get("runner_host"),
                "lease_until": info.get("lease_until"),
                "lease_heartbeat": info.get("lease_heartbeat"),
            }
        )
        self._copy_gpu_schedule_info(task, info)
        self._copy_gpu_wait_info(task, info)
        loaded_kind, loaded_config, loaded_text, load_error = read_task_payload(task["dir"], info)
        task["task_kind"] = loaded_kind or task.get("task_kind", TASK_KIND_CONFIG)
        task["config"] = loaded_config
        task["config_text"] = loaded_text
        task["_load_error"] = load_error
        if mtime_ns is not None:
            task["_mtime_ns"] = mtime_ns
            task["_mtime"] = mtime_ns / 1_000_000_000
        self._refresh_derived_fields(task)

    def _refresh_derived_fields(self, task: Dict[str, Any]) -> None:
        preview_text, search_text = build_task_preview_and_search(
            task_kind=str(task.get("task_kind", TASK_KIND_CONFIG) or TASK_KIND_CONFIG),
            config=task.get("config", {}) or {},
            config_text=str(task.get("config_text", "") or ""),
            task_name=str(task.get("name", "") or ""),
            notes=str(task.get("notes", "") or ""),
        )
        task["preview_text"] = preview_text
        task["search_text"] = search_text

    def _rebuild_indexes_locked(self) -> None:
        self._tasks_by_name = {task["name"]: task for task in self.tasks if task and task.get("name")}

    @staticmethod
    def _task_matches_identifier(task: Dict[str, Any], identifiers: set[str]) -> bool:
        return str(task.get("name")) in identifiers

    def _recompute_processing_flag_locked(self) -> None:
        """Sleep the scheduler when nothing is queued or running."""
        has_queued = any(task and task.get("status") == "queued" for task in self.tasks)
        self.is_processing = bool(self._running_ids or has_queued)
