"""Task registry, disk sync, and background scheduling for Pyruns."""

from __future__ import annotations

import atexit
import os
import shutil
import threading
import time
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from typing import Any, Callable, Dict, List

from pyruns._config import (
    CONFIG_FILENAME,
    ERROR_LOG_FILENAME,
    RUN_LOGS_DIR,
    TASK_INFO_FILENAME,
    TASKS_DIR,
    TRASH_DIR,
)
from pyruns.core.executor import run_task_worker
from pyruns.utils import get_logger, get_now_str
from pyruns.utils.config_utils import load_yaml
from pyruns.utils.info_io import load_task_info, normalize_run_history, save_task_info
from pyruns.utils.process_utils import is_pid_running, kill_process

logger = get_logger(__name__)


class TaskManager:
    """Central task registry, scheduler, and UI notification source."""

    def __init__(self, tasks_dir: str | None = None, lazy_scan: bool = True):
        if tasks_dir is None:
            from pyruns._config import ROOT_DIR

            tasks_dir = os.path.join(ROOT_DIR, TASKS_DIR)

        self.tasks_dir = tasks_dir
        self.tasks: List[Dict[str, Any]] = []
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

        import sys

        if "nicegui" in sys.modules:
            try:
                from nicegui import app

                if hasattr(app, "on_shutdown"):
                    app.on_shutdown(self._cleanup_on_shutdown)
            except Exception:
                pass

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
                self.is_processing = False
            return

        subdirs = sorted(
            (
                name
                for name in os.listdir(self.tasks_dir)
                if os.path.isdir(os.path.join(self.tasks_dir, name)) and name != TRASH_DIR
            ),
            key=lambda name: os.path.getmtime(os.path.join(self.tasks_dir, name)),
            reverse=True,
        )

        new_tasks = []
        for dir_name in subdirs:
            task = self._load_task_dir(dir_name)
            if task is not None:
                new_tasks.append(task)

        with self._lock:
            self.tasks = new_tasks
            self._recompute_processing_flag_locked()
        logger.debug("scan_disk completed: %d tasks found", len(new_tasks))

    def _load_task_dir(self, dir_name: str) -> Dict[str, Any] | None:
        """Load one task folder into the normalized task dict shape."""
        task_dir = os.path.join(self.tasks_dir, dir_name)
        info_path = os.path.join(task_dir, TASK_INFO_FILENAME)
        if not os.path.exists(info_path):
            return None

        config_path = next(
            (
                path
                for path in (
                    os.path.join(task_dir, CONFIG_FILENAME),
                    os.path.join(task_dir, "parameters.yaml"),
                )
                if os.path.exists(path)
            ),
            None,
        )
        if not config_path:
            return None

        try:
            info = load_task_info(task_dir)
            if not info:
                return None
        except Exception as exc:
            logger.error("Error loading info for %s: %s", dir_name, exc)
            return None

        task_name = info.get("name")
        if info.get("status") == "running" and task_name not in self._running_ids:
            pid = self._latest_pid(info)
            if not (pid and is_pid_running(pid)):
                self._mark_failed_on_disk({"dir": task_dir})
                info["status"] = "failed"
                logger.warning("%s: running but process gone; marked failed", dir_name)

        try:
            config_data = load_yaml(config_path)
        except Exception as exc:
            logger.error("Error loading config for %s: %s", dir_name, exc)
            return None

        try:
            mtime_ns = os.stat(info_path).st_mtime_ns
        except OSError:
            mtime_ns = 0

        task = {
            "dir": task_dir.replace("\\", "/"),
            "name": info.get("name", dir_name),
            "status": info.get("status", "pending"),
            "created_at": info.get("created_at"),
            "config": config_data,
            "log": "",
            "progress": info.get("progress", 0.0),
            "env": info.get("env", {}),
            "pinned": info.get("pinned", False),
            "script": info.get("script"),
            "run_mode": info.get("run_mode", "config"),
            "start_times": info.get("start_times", []),
            "finish_times": info.get("finish_times", []),
            "pids": info.get("pids", []),
            "records": len(info.get("records", [])),
            "_mtime": (mtime_ns / 1_000_000_000) if mtime_ns else 0.0,
            "_mtime_ns": mtime_ns,
        }
        pending_run_index = info.get("run_index", info.get("_run_index"))
        if pending_run_index:
            task["run_index"] = pending_run_index
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
                or (target_ids and task["name"] in target_ids)
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
                    existing = self._find(task["name"])
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
            self._recompute_processing_flag_locked()
        self.trigger_update()

    def add_tasks(self, task_objs: List[Dict[str, Any]]) -> None:
        with self._lock:
            for task in reversed(task_objs):
                self.tasks.insert(0, task)
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

        to_sync: list[tuple[Dict[str, Any], int]] = []
        with self._lock:
            for task in self.tasks:
                if task and task["name"] in task_ids:
                    task["status"] = "queued"
                    run_index = len(task.get("start_times", [])) + 1
                    task["run_index"] = run_index
                    to_sync.append((task, run_index))
            self._recompute_processing_flag_locked()
        self.trigger_update()

        def _job() -> None:
            logger.info("Queued %d task(s) for execution. Processing IO...", len(task_ids))
            for task, run_index in to_sync:
                self._sync_status_to_disk(task, "queued", run_index=run_index)

        threading.Thread(target=_job, daemon=True).start()

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
            for task in self.tasks:
                if task and task["name"] == task_id:
                    task["status"] = "running"
                    self._clean_aborted_runs(task)
                    run_index = len(task.get("start_times", [])) + 1
                    task["run_index"] = run_index
                    self._running_ids.add(task["name"])
                    self._recompute_processing_flag_locked()
                    target = task
                    break

        if not target:
            return

        self.trigger_update()

        try:
            with self._executor_lock:
                if not self._independent_executor:
                    cls = ProcessPoolExecutor if execution_mode == "process" else ThreadPoolExecutor
                    self._independent_executor = cls(max_workers=32)

            future = self._independent_executor.submit(
                run_task_worker,
                target["dir"],
                target["name"],
                target["created_at"],
                target["config"],
                target.get("env", {}),
                run_index,
            )
            future.add_done_callback(lambda fut, tid=target["name"]: self._on_task_done(fut, tid))
            logger.debug("Submitted task %s to independent executor", target["name"])
        except Exception as exc:
            with self._lock:
                self._running_ids.discard(target["name"])
                target["status"] = "failed"
                self._mark_failed_on_disk(target)
                self._recompute_processing_flag_locked()
            logger.error("Failed to submit task %s: %s", target["name"], exc)

        def _job() -> None:
            self._sync_status_to_disk(target, "running", run_index=run_index)

        threading.Thread(target=_job, daemon=True).start()

    def rerun_task(self, task_id: str) -> bool:
        """Queue a completed or failed task again."""
        with self._lock:
            target = self._find(task_id)
            if not target or target["status"] not in ("completed", "failed"):
                return False

            self._clean_aborted_runs(target)
            run_index = len(target.get("start_times", [])) + 1
            target["status"] = "queued"
            target["run_index"] = run_index
            self._sync_status_to_disk(target, "queued", run_index=run_index)
            self._recompute_processing_flag_locked()

        self.trigger_update()
        return True

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a queued or running task."""
        with self._lock:
            target = self._find(task_id)
            if not target or target["status"] not in ("queued", "running"):
                return False

            if target["status"] == "running":
                pid = self._latest_pid_from_disk(target)
                if pid:
                    kill_process(int(pid))
                self._running_ids.discard(task_id)

            target["status"] = "failed"
            self._mark_failed_on_disk(target)
            self._recompute_processing_flag_locked()
            logger.info("Cancelled task %s", task_id[:8])

        self.trigger_update()
        return True

    def delete_tasks(self, task_ids: List[str]) -> None:
        """Soft-delete tasks by moving folders into .trash."""
        targets = []
        with self._lock:
            for task_id in task_ids:
                target = self._find(task_id)
                if not target:
                    continue
                if target["status"] in ("running", "queued"):
                    if target["status"] == "running":
                        pid = self._latest_pid_from_disk(target)
                        if pid:
                            kill_process(int(pid))
                        self._running_ids.discard(task_id)
                    target["status"] = "failed"
                    self._mark_failed_on_disk(target)
                self.tasks.remove(target)
                targets.append(target)
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
        while True:
            try:
                if self.refresh_from_disk():
                    now = time.time()
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

                try:
                    future = self._executor.submit(
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
                        "Submitted task %s to executor (running=%d/%d)",
                        target["name"],
                        len(self._running_ids),
                        self.max_workers,
                    )
                except Exception as exc:
                    with self._lock:
                        self._running_ids.discard(target["name"])
                        target["status"] = "failed"
                        self._mark_failed_on_disk(target)
                        self._recompute_processing_flag_locked()
                    logger.error("Failed to submit task %s: %s", target["name"], exc)
            except Exception as exc:
                logger.error("Scheduler error: %s", exc, exc_info=True)
                time.sleep(1)

            time.sleep(0.2)

    def _ensure_executor(self) -> None:
        """Create or recreate the batch executor when mode/worker count changes."""
        with self._executor_lock:
            workers = max(1, int(self.max_workers))
            changed = (
                self._executor_mode != self.execution_mode
                or self._executor_workers != workers
            )
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
                    run_index = task.pop("run_index", task.pop("_run_index", 1))
                    task["status"] = "running"
                    self._running_ids.add(task["name"])
                    self._recompute_processing_flag_locked()
                    return task, run_index
            self._recompute_processing_flag_locked()
        return None, 1

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

        with self._lock:
            self._running_ids.discard(task_id)
            task = self._find(task_id)
            if not task:
                self._recompute_processing_flag_locked()
                return

            try:
                info = load_task_info(task["dir"])
                if info:
                    self._apply_info_to_task(task, info)
            except Exception:
                pass

            if worker_error and task["status"] in ("running", "queued"):
                task["status"] = "failed"
                disk_info = load_task_info(task["dir"])
                disk_info["status"] = "failed"
                save_task_info(task["dir"], disk_info)

            self._recompute_processing_flag_locked()

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
                self._mark_failed_on_disk(task)
                log_dir = os.path.join(task["dir"], RUN_LOGS_DIR)
                os.makedirs(log_dir, exist_ok=True)
                error_log = os.path.join(log_dir, ERROR_LOG_FILENAME)
                try:
                    with open(error_log, "a", encoding="utf-8") as handle:
                        handle.write(
                            f"\n[{get_now_str()}] Task forcibly terminated due to system shutdown or Ctrl+C.\n"
                        )
                except Exception as exc:
                    logger.error("Failed to write error.log for %s: %s", task["name"], exc)
            self._recompute_processing_flag_locked()
        finally:
            self._lock.release()

    def _find(self, task_id: str) -> Dict[str, Any] | None:
        return next((task for task in self.tasks if task["name"] == task_id), None)

    @staticmethod
    def _latest_pid(info: Dict[str, Any]) -> Any:
        pids = info.get("pids", [])
        return pids[-1] if isinstance(pids, list) and pids else None

    def _latest_pid_from_disk(self, task: Dict[str, Any]) -> Any:
        task_info = load_task_info(task["dir"])
        return self._latest_pid(task_info) if task_info else None

    def _clean_aborted_runs(self, task: Dict[str, Any]) -> None:
        """Trim incomplete run history and remove stale runN.log files."""
        task_info = load_task_info(task["dir"])
        if not task_info:
            return

        valid_count = normalize_run_history(task_info)
        log_dir = os.path.join(task["dir"], RUN_LOGS_DIR)
        if os.path.exists(log_dir):
            index = valid_count + 1
            while True:
                bad_log = os.path.join(log_dir, f"run{index}.log")
                if not os.path.exists(bad_log):
                    break
                try:
                    os.remove(bad_log)
                except OSError:
                    pass
                index += 1

        save_task_info(task["dir"], task_info)
        self._apply_info_to_task(task, task_info)
        task["run_index"] = task_info.get("run_index", valid_count)

    def _sync_status_to_disk(self, task: Dict[str, Any], status: str, run_index: int = 1) -> None:
        """Persist transient queue/running status changes."""
        task_info = load_task_info(task["dir"])
        if not task_info:
            return
        task_info["status"] = status
        task_info["run_index"] = run_index
        task_info.pop("_run_index", None)
        save_task_info(task["dir"], task_info)

    def _mark_failed_on_disk(self, task: Dict[str, Any]) -> None:
        """Persist a failed state and normalize incomplete history."""
        task_info = load_task_info(task["dir"])
        if not task_info:
            return
        task_info["status"] = "failed"
        normalize_run_history(task_info)
        save_task_info(task["dir"], task_info)
        self._apply_info_to_task(task, task_info)
        task["status"] = "failed"

    @staticmethod
    def _task_snapshot(task: Dict[str, Any]) -> tuple:
        """Compact comparison tuple for change detection."""
        return (
            task.get("status"),
            task.get("progress"),
            tuple(task.get("start_times", [])),
            tuple(task.get("finish_times", [])),
            tuple(task.get("pids", [])),
            task.get("records"),
            task.get("pinned"),
            task.get("run_mode"),
        )

    def _apply_info_to_task(
        self,
        task: Dict[str, Any],
        info: Dict[str, Any],
        *,
        mtime_ns: int | None = None,
    ) -> None:
        """Copy task_info.json fields used by UI and scheduler."""
        task.update({
            "status": info.get("status", task.get("status", "pending")),
            "progress": info.get("progress", task.get("progress", 0.0)),
            "env": info.get("env", task.get("env", {})),
            "pinned": info.get("pinned", task.get("pinned", False)),
            "script": info.get("script", task.get("script")),
            "run_mode": info.get("run_mode", task.get("run_mode", "config")),
            "start_times": info.get("start_times", []),
            "finish_times": info.get("finish_times", []),
            "pids": info.get("pids", []),
            "records": len(info.get("records", [])),
            "run_index": info.get("run_index", info.get("_run_index", task.get("run_index", 0))),
        })
        if mtime_ns is not None:
            task["_mtime_ns"] = mtime_ns
            task["_mtime"] = mtime_ns / 1_000_000_000

    def _recompute_processing_flag_locked(self) -> None:
        """Sleep the scheduler when nothing is queued or running."""
        has_queued = any(task and task.get("status") == "queued" for task in self.tasks)
        self.is_processing = bool(self._running_ids or has_queued)
