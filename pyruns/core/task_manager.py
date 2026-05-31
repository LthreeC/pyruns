"""Task registry, disk sync, and background scheduling for Pyruns."""

from __future__ import annotations

import atexit
import copy
import os
import shutil
import threading
import time
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from pyruns._config import (
    ERROR_LOG_FILENAME,
    RUN_LOGS_DIR,
    TASK_KIND_CONFIG,
    TASK_INFO_FILENAME,
    TASKS_DIR,
    TRASH_DIR,
)
from pyruns.core.executor import run_task_worker
from pyruns.utils import get_logger, get_now_str
from pyruns.utils.info_io import (
    ensure_run_slot,
    load_task_info,
    run_slot_count,
    update_task_info,
    validate_task_name,
)
from pyruns.utils.process_utils import is_pid_running, kill_process
from pyruns.utils.events import event_sys
from pyruns.utils.task_files import (
    build_task_preview_and_search,
    normalize_task_kind,
    read_task_payload,
    resolve_task_config_file,
)

logger = get_logger(__name__)


class TaskManager:
    """Central task registry, scheduler, and UI notification source."""

    def __init__(self, tasks_dir: str | None = None, lazy_scan: bool = True):
        if tasks_dir is None:
            from pyruns._config import ROOT_DIR

            tasks_dir = os.path.join(ROOT_DIR, TASKS_DIR)

        self.tasks_dir = tasks_dir
        self.tasks: List[Dict[str, Any]] = []
        self._tasks_by_name: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._observer_lock = threading.Lock()
        self._executor_lock = threading.Lock()

        self._observers: List[Callable[[], None]] = []
        self._executor = None
        self._independent_executor = None
        self._executor_mode = None
        self._executor_workers = 0

        self.execution_mode = "thread"
        self.max_workers = 1
        self.is_processing = False
        self._running_ids: set[str] = set()

        logger.info("TaskManager initialised  root=%s", tasks_dir)
        if lazy_scan:
            self.scan_disk_async()
        else:
            self.scan_disk()

        threading.Thread(target=self._scheduler_loop, daemon=True).start()

        atexit.register(self._cleanup_on_shutdown)

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
    def serialize_task(task: Dict[str, Any] | None) -> Dict[str, Any] | None:
        """Return a detached task copy suitable for APIs and read-only consumers."""
        if task is None:
            return None
        data = copy.deepcopy(task)
        data["dir"] = str(data.get("dir", "")).replace("\\", "/")
        return data

    def list_tasks(self) -> List[Dict[str, Any]]:
        """Return detached copies of the current task list."""
        with self._lock:
            tasks = list(self.tasks)
        return [
            serialized
            for serialized in (self.serialize_task(task) for task in tasks)
            if serialized is not None
        ]

    def get_task(self, identifier: str) -> Dict[str, Any] | None:
        """Return a detached task copy by name."""
        with self._lock:
            task = self._tasks_by_name.get(identifier)
        return self.serialize_task(task)

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
            return

        # Use os.scandir for fewer syscalls (isdir + mtime in one stat)
        try:
            entries = []
            with os.scandir(self.tasks_dir) as it:
                for entry in it:
                    if entry.is_dir() and entry.name != TRASH_DIR:
                        try:
                            mtime = entry.stat().st_mtime
                        except OSError:
                            mtime = 0.0
                        entries.append((entry.name, mtime))
            entries.sort(key=lambda x: x[1], reverse=True)
            subdirs = [name for name, _ in entries]
        except OSError:
            subdirs = []

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
        logger.debug("scan_disk completed: %d tasks found", len(new_tasks))

    def _load_task_dir(self, dir_name: str) -> Dict[str, Any] | None:
        """Load one task folder into the normalized task dict shape."""
        task_dir = os.path.join(self.tasks_dir, dir_name)
        info_path = os.path.join(task_dir, TASK_INFO_FILENAME)
        if not os.path.exists(info_path):
            return None

        try:
            info = load_task_info(task_dir)
            if not info:
                return None
        except Exception as exc:
            logger.error("Error loading info for %s: %s", dir_name, exc)
            return None

        task_kind, config_data, config_text, load_error = read_task_payload(task_dir, info)

        task_name = dir_name
        if info.get("status") == "running" and task_name not in self._running_ids:
            pid = self._latest_pid(info)
            if not (pid and is_pid_running(pid)):
                self._mark_failed_on_disk(
                    {"name": task_name, "dir": task_dir, "run_index": run_slot_count(info)},
                )
                info = load_task_info(task_dir)
                logger.warning("%s: running but process gone; marked failed", dir_name)

        try:
            mtime_ns = os.stat(info_path).st_mtime_ns
        except OSError:
            mtime_ns = 0

        task = {
            "dir": task_dir.replace("\\", "/"),
            "name": task_name,
            "status": info.get("status", "pending"),
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
            "start_times": info.get("start_times", []),
            "finish_times": info.get("finish_times", []),
            "pids": info.get("pids", []),
            "records": info.get("records", []),
            "tracks": info.get("tracks", []),
            "notes": info.get("notes", ""),
            "_load_error": load_error,
            "_mtime": (mtime_ns / 1_000_000_000) if mtime_ns else 0.0,
            "_mtime_ns": mtime_ns,
        }
        pending_run_index = info.get("run_index", info.get("_run_index"))
        if pending_run_index:
            task["run_index"] = int(pending_run_index)
        self._refresh_derived_fields(task)
        return task

    def refresh_from_disk(
        self,
        task_ids: List[str] | None = None,
        force_all: bool = False,
        check_all: bool = False,
    ) -> bool:
        """Refresh active or requested tasks from task_info.json files."""
        with self._lock:
            current = list(self.tasks)

        has_changed = False
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
            if not os.path.exists(info_path):
                continue

            try:
                mtime_ns = os.stat(info_path).st_mtime_ns
                if not force_all and task.get("_mtime_ns") == mtime_ns:
                    continue
                info = load_task_info(task["dir"])
                if not info:
                    continue

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

    def add_task(self, task_obj: Dict[str, Any]) -> None:
        with self._lock:
            self.tasks.insert(0, task_obj)
            self._rebuild_indexes_locked()
            self._recompute_processing_flag_locked()
        self.trigger_update()

    def add_tasks(self, task_objs: List[Dict[str, Any]]) -> None:
        with self._lock:
            for task in reversed(task_objs):
                self.tasks.insert(0, task)
            self._rebuild_indexes_locked()
            self._recompute_processing_flag_locked()
        self.trigger_update()

    def start_batch_tasks(
        self,
        task_ids: List[str],
        execution_mode: str | None = None,
        max_workers: int | None = None,
    ) -> None:
        """Queue a batch of tasks for scheduler-driven execution."""
        if execution_mode:
            self.execution_mode = execution_mode
        if max_workers is not None:
            self.max_workers = max(1, int(max_workers))

        to_sync: list[tuple[str, str, int]] = []
        to_submit: list[tuple[Dict[str, Any], int]] = []
        with self._lock:
            available_slots = max(0, int(self.max_workers) - len(self._running_ids))
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
                run_index = max(int(task.get("run_index", 0) or 0), len(task.get("start_times", []))) + 1
                task["run_index"] = run_index
                if available_slots > 0:
                    task["status"] = "running"
                    self._running_ids.add(task["name"])
                    to_submit.append((task, run_index))
                    to_sync.append((task["name"], "running", run_index))
                    available_slots -= 1
                else:
                    task["status"] = "queued"
                    to_sync.append((task["name"], "queued", run_index))
            self._recompute_processing_flag_locked()
        self.trigger_update()

        logger.info(
            "Prepared %d task(s) for execution (%d immediate, %d queued)",
            len(to_sync),
            len(to_submit),
            max(0, len(to_sync) - len(to_submit)),
        )
        for task_id, status, run_index in to_sync:
            self._sync_status_to_disk(task_id, status, run_index=run_index)
        for task, run_index in to_submit:
            self._submit_task(task, run_index, independent=False)

    def start_task_now(
        self,
        task_id: str,
        execution_mode: str | None = None,
    ) -> None:
        """Immediately submit a single task outside the batch queue."""
        execution_mode = execution_mode or self.execution_mode

        target = None
        run_index = 1
        with self._lock:
            target = self._resolve_identifier_locked(task_id)
            if target:
                if target.get("status") in ("running", "queued"):
                    logger.info("Skip starting active task %s", target["name"])
                    return
                if target.get("_load_error"):
                    logger.warning("Skip running %s: %s", target["name"], target["_load_error"])
                    return
                target["status"] = "running"
                run_index = max(int(target.get("run_index", 0) or 0), len(target.get("start_times", []))) + 1
                target["run_index"] = run_index
                self._running_ids.add(target["name"])
                self._recompute_processing_flag_locked()

        if not target:
            return

        self.trigger_update()
        self._submit_task(target, run_index, independent=True, execution_mode=execution_mode)

    def rerun_task(self, task_id: str) -> bool:
        """Queue a completed or failed task again."""
        with self._lock:
            target = self._resolve_identifier_locked(task_id)
            if not target or target["status"] not in ("completed", "failed"):
                return False
            if target.get("_load_error"):
                logger.warning("Skip re-queuing %s: %s", target["name"], target["_load_error"])
                return False

            run_index = max(int(target.get("run_index", 0) or 0), len(target.get("start_times", []))) + 1
            target["status"] = "queued"
            target["run_index"] = run_index
            self._recompute_processing_flag_locked()

        self._sync_status_to_disk(target["name"], "queued", run_index=run_index)
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
            task_name = str((item or {}).get("name", "")).strip()
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

    def update_task_notes(self, task_name: str, notes: str) -> tuple[bool, str]:
        """Persist task notes and refresh derived search/preview fields."""
        with self._lock:
            target = self._resolve_identifier_locked(task_name)
            if not target:
                return False, "Task not found"
            task_dir = target["dir"]

        def _apply(task_info: Dict[str, Any]) -> None:
            task_info["notes"] = str(notes or "")

        updated = update_task_info(task_dir, _apply)
        with self._lock:
            current = self._resolve_identifier_locked(task_name)
            if current:
                self._apply_info_to_task(current, updated)
        self.trigger_update()
        return True, str(updated.get("notes", "") or "")

    def update_task_env(self, task_name: str, env: Dict[str, Any]) -> tuple[bool, Dict[str, Any] | str]:
        """Persist task env vars and sync in-memory task state."""
        with self._lock:
            target = self._resolve_identifier_locked(task_name)
            if not target:
                return False, "Task not found"
            task_dir = target["dir"]

        normalized_env = {str(k): str(v) for k, v in (env or {}).items() if str(k)}

        def _apply(task_info: Dict[str, Any]) -> None:
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
        new_name = (new_name or "").strip()
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
                os.rename(old_dir, new_dir)
            except OSError as exc:
                return False, str(exc)

            try:
                def _apply(info: Dict[str, Any]) -> None:
                    info["name"] = new_name

                update_task_info(new_dir, _apply, raise_error=True)
            except Exception as exc:
                try:
                    os.rename(new_dir, old_dir)
                except OSError:
                    pass
                return False, str(exc)

            target["dir"] = new_dir.replace("\\", "/")
            target["name"] = new_name
            self._refresh_derived_fields(target)
            self._rebuild_indexes_locked()

        self.trigger_update()
        event_sys.emit("on_task_rename", old_name, new_name)
        return True, new_name

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a queued or running task."""
        target_name = ""
        target_ref: Dict[str, Any] | None = None
        previous_status = ""
        was_running = False

        with self._lock:
            target = self._resolve_identifier_locked(task_id)
            if not target or target["status"] not in ("queued", "running"):
                return False
            previous_status = str(target["status"])
            was_running = previous_status == "running"
            target_name = target["name"]
            target_ref = target

        if target_ref is None:
            return False

        if was_running:
            pid = self._latest_pid_from_disk(target_ref)
            if pid:
                kill_process(int(pid))
            try:
                self._persist_pending_stop_summary(
                    target_ref,
                    event="stopped",
                    reason="cancelled_by_user",
                    detail_lines=[f"previous_status={previous_status}"],
                )
            except TimeoutError as exc:
                logger.warning("Could not persist cancel summary for %s yet: %s", target_name, exc)
        else:
            try:
                self._mark_failed_on_disk(
                    target_ref,
                    event="stopped",
                    reason="cancelled_by_user",
                    detail_lines=[f"previous_status={previous_status}"],
                )
            except TimeoutError as exc:
                logger.warning("Could not persist queued cancel state for %s yet: %s", target_name, exc)

        with self._lock:
            current = self._resolve_identifier_locked(target_name)
            if current:
                current["status"] = "failed"
                if was_running:
                    self._running_ids.discard(target_name)
            self._recompute_processing_flag_locked()
            logger.info("Cancelled task %s", target_name)
        self.trigger_update()
        return True

    def delete_tasks(self, task_ids: List[str]) -> None:
        """Soft-delete tasks by moving folders into .trash."""
        targets = []
        with self._lock:
            for identifier in task_ids:
                target = self._resolve_identifier_locked(identifier)
                if not target or target in targets:
                    continue
                if target["status"] in ("running", "queued"):
                    previous_status = str(target["status"])
                    if target["status"] == "running":
                        pid = self._latest_pid_from_disk(target)
                        if pid:
                            kill_process(int(pid))
                        self._running_ids.discard(target["name"])
                    target["status"] = "failed"
                    self._mark_failed_on_disk(
                        target,
                        event="stopped",
                        reason="deleted_while_active",
                        detail_lines=[f"previous_status={previous_status}"],
                    )
                if target in self.tasks:
                    self.tasks.remove(target)
                targets.append(target)
            self._rebuild_indexes_locked()
            self._recompute_processing_flag_locked()

        if not targets:
            return

        self.trigger_update()

        trash_dir = os.path.join(self.tasks_dir, TRASH_DIR)
        os.makedirs(trash_dir, exist_ok=True)

        for target in targets:
            folder = os.path.basename(target["dir"])
            destination = os.path.join(trash_dir, folder)
            if os.path.exists(destination):
                destination = os.path.join(trash_dir, f"{folder}_{get_now_str()}")

            for attempt in range(3):
                try:
                    shutil.move(target["dir"], destination)
                    break
                except Exception as exc:
                    if attempt < 2:
                        time.sleep(0.2)
                    else:
                        logger.error("Error moving task to trash after retries: %s", exc)
                        try:
                            shutil.rmtree(target["dir"])
                        except Exception:
                            pass

    def _scheduler_loop(self) -> None:
        """Submit queued tasks up to max_workers and keep UI state fresh."""
        last_trigger = 0.0
        last_refresh = 0.0
        while True:
            try:
                # Only refresh from disk if we have active tasks or it's been >1s
                now = time.time()
                should_refresh = (
                    self._running_ids or
                    self.is_processing or
                    (now - last_refresh >= 1.0)
                )

                if should_refresh:
                    last_refresh = now
                    if self.refresh_from_disk():
                        if now - last_trigger >= 1.0:
                            last_trigger = now
                            self.trigger_update()

                if not self.is_processing:
                    time.sleep(0.5)
                    continue

                self._ensure_executor()

                if len(self._running_ids) >= self.max_workers:
                    time.sleep(0.1)
                    continue

                target, run_index = self._pick_queued_task()
                if not target:
                    with self._lock:
                        self._recompute_processing_flag_locked()
                    time.sleep(0.1)
                    continue

                self._submit_task(target, run_index, independent=False)
            except Exception as exc:
                logger.error("Scheduler error: %s", exc, exc_info=True)
                time.sleep(1)

            time.sleep(0.2)

    def _ensure_executor(self) -> None:
        """Create or recreate the batch executor when mode/worker count changes."""
        with self._executor_lock:
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

    def _pick_queued_task(self) -> tuple[Dict[str, Any] | None, int]:
        """Pick the next queued task and mark it running."""
        with self._lock:
            for task in self.tasks:
                if task and task["status"] == "queued":
                    run_index = int(task.get("run_index", 1) or 1)
                    task["status"] = "running"
                    self._running_ids.add(task["name"])
                    self._recompute_processing_flag_locked()
                    return task, run_index
            self._recompute_processing_flag_locked()
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

        self._sync_status_to_disk(target["name"], "running", run_index=run_index)

        try:
            if independent:
                mode = execution_mode or self.execution_mode
                with self._executor_lock:
                    if not self._independent_executor:
                        cls = ProcessPoolExecutor if mode == "process" else ThreadPoolExecutor
                        self._independent_executor = cls(max_workers=32)
                executor = self._independent_executor
            else:
                self._ensure_executor()
                executor = self._executor

            assert executor is not None
            future = executor.submit(
                run_task_worker,
                target["dir"],
                target["name"],
                target["created_at"],
                target["config"],
                target.get("env", {}),
                run_index,
            )
            future.add_done_callback(lambda fut, tid=target["name"]: self._on_task_done(fut, tid))
            logger.debug(
                "Submitted task %s to %s executor (running=%d/%d)",
                target["name"],
                "independent" if independent else "batch",
                len(self._running_ids),
                self.max_workers,
            )
        except Exception as exc:
            with self._lock:
                self._running_ids.discard(target["name"])
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
            self._running_ids.discard(task_id)
            task = self._tasks_by_name.get(task_id)
            if not task:
                self._recompute_processing_flag_locked()
                self.trigger_update()
                return

            try:
                info = load_task_info(task["dir"])
                if info:
                    self._apply_info_to_task(task, info)
            except Exception:
                pass

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
        logger.info("System shutting down; cleaning up stuck task states...")
        acquired = self._lock.acquire(timeout=2.0)
        if not acquired:
            logger.warning("Skip shutdown cleanup: task manager lock not acquired in time.")
            return

        try:
            for task in self.tasks:
                if not task or task["status"] not in ("running", "queued"):
                    continue
                task["status"] = "failed"
                self._mark_failed_on_disk(
                    task,
                    event="stopped",
                    reason="system_shutdown",
                    detail_lines=["detail=Task forcibly terminated due to system shutdown or Ctrl+C."],
                )
            self._recompute_processing_flag_locked()
        finally:
            self._lock.release()

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

    def _latest_pid_from_disk(self, task: Dict[str, Any]) -> Any:
        task_info = load_task_info(task["dir"])
        return self._latest_pid(task_info) if task_info else None

    def _sync_status_to_disk(self, identifier: str, status: str, run_index: int = 1) -> None:
        """Persist transient queue/running status changes."""
        with self._lock:
            task = self._resolve_identifier_locked(identifier)
            if not task:
                return
            task_dir = task["dir"]

        def _apply(task_info: Dict[str, Any]) -> None:
            task_info["status"] = status
            task_info["run_index"] = run_index

        update_task_info(task_dir, _apply)

    def _append_error_summary(
        self,
        task_dir: str,
        *,
        title: str,
        detail_lines: List[str],
    ) -> None:
        """Append a structured failure/cancel summary block into error.log."""

        log_dir = os.path.join(task_dir, RUN_LOGS_DIR)
        os.makedirs(log_dir, exist_ok=True)
        error_log = os.path.join(log_dir, ERROR_LOG_FILENAME)
        block = (
            f"\n\n{'=' * 70}\n"
            f"[PYRUNS] {title}\n"
            + "\n".join(detail_lines)
            + f"\n{'=' * 70}\n"
        )
        try:
            with open(error_log, "a", encoding="utf-8") as handle:
                handle.write(block)
        except Exception as exc:
            logger.error("Failed to write error.log for %s: %s", task_dir, exc)

    def _persist_pending_stop_summary(
        self,
        task: Dict[str, Any],
        *,
        event: str,
        reason: str,
        detail_lines: List[str] | None = None,
    ) -> None:
        """Store a stop summary on the active run so the worker can flush one final block."""

        task_dir = task["dir"]
        finish_now = get_now_str()
        run_index = int(task.get("run_index", 0) or 0)

        def _apply(task_info: Dict[str, Any]) -> None:
            slot_count = run_slot_count(task_info)
            target_index = max(run_index, slot_count)
            if target_index > 0:
                slot = ensure_run_slot(task_info, target_index)
                if not task_info["finish_times"][slot]:
                    task_info["finish_times"][slot] = finish_now
            task_info["status"] = "failed"
            task_info["progress"] = 0.0
            task_info["_pending_stop_summary"] = {
                "run_index": max(target_index, 1),
                "event": event,
                "reason": reason,
                "detail_lines": list(detail_lines or []),
            }

        updated = update_task_info(task_dir, _apply)
        if "status" in task:
            self._apply_info_to_task(task, updated)
            task["status"] = "failed"

    def _mark_failed_on_disk(
        self,
        task: Dict[str, Any],
        *,
        event: str = "failed",
        reason: str | None = None,
        detail_lines: List[str] | None = None,
    ) -> None:
        """Persist a failed state and finalize the active run slot if needed."""
        task_dir = task["dir"]
        finish_now = get_now_str()
        run_index = int(task.get("run_index", 0) or 0)

        def _apply(task_info: Dict[str, Any]) -> None:
            slot_count = run_slot_count(task_info)
            target_index = max(run_index, slot_count)
            should_finalize_slot = slot_count > 0 or task_info.get("status") == "running"
            if should_finalize_slot and target_index > 0:
                slot = ensure_run_slot(task_info, target_index)
                if not task_info["finish_times"][slot]:
                    task_info["finish_times"][slot] = finish_now
            task_info["status"] = "failed"
            task_info["progress"] = 0.0

        updated = update_task_info(task_dir, _apply)
        if reason or detail_lines:
            display_run_index = max(run_index, int(updated.get("run_index", 0) or 0), 1)
            lines: List[str] = []
            if reason:
                lines.append(f"reason={reason}")
            lines.extend(detail_lines or [])
            self._append_error_summary(
                task_dir,
                title=f"Run #{display_run_index} {event} at {finish_now}",
                detail_lines=lines,
            )
        if "status" in task:
            self._apply_info_to_task(task, updated)
            task["status"] = "failed"

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
            tuple(repr(item) for item in (task.get("records", []) or [])),
            task.get("pinned"),
            task.get("task_order"),
            task.get("task_kind"),
            task.get("config_file"),
            task.get("notes", ""),
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
                "records": info.get("records", []),
                "tracks": info.get("tracks", []),
                "notes": info.get("notes", ""),
                "run_index": int(info.get("run_index", info.get("_run_index", task.get("run_index", 0))) or 0),
            }
        )
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
