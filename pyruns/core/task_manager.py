"""
Task Manager — scans disk, manages task lifecycle, schedules execution.

Responsibilities:
  • ``scan_disk()``         – full directory scan, build in-memory task list
  • ``refresh_from_disk()`` – lightweight status update for active tasks only
  • CRUD operations         – add / start / run again / cancel / delete
  • Scheduler loop          – picks queued tasks and submits them to an executor
"""
import os
import time
import threading
import shutil
from typing import List, Dict, Any, Callable
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, Future

from pyruns._config import (
    TASKS_DIR, TASK_INFO_FILENAME, CONFIG_FILENAME,
    TRASH_DIR, RUN_LOGS_DIR, ERROR_LOG_FILENAME,
)
from pyruns.utils.config_utils import load_yaml
from pyruns.utils.process_utils import is_pid_running, kill_process
from pyruns.utils.info_io import load_task_info, save_task_info, normalize_run_history
from pyruns.core.executor import run_task_worker
from pyruns.utils import get_logger, get_now_str

logger = get_logger(__name__)


class TaskManager:
    """Central task registry and scheduler.

    Parameters
    ----------
    tasks_dir : str
        Directory that contains task sub-folders (each with ``task_info.json``).
    """

    def __init__(self, tasks_dir: str = None, lazy_scan: bool = True):
        if tasks_dir is None:
            from pyruns._config import ROOT_DIR, TASKS_DIR
            tasks_dir = os.path.join(ROOT_DIR, TASKS_DIR)
            
        self.tasks_dir = tasks_dir
        self.tasks: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._observer_lock = threading.Lock()

        # Execution state
        self.is_processing = False

        # Observers for reactive UI
        self._observers: List[Callable[[], None]] = []

        # Executor pool (created lazily)
        # Batch executor state
        self.execution_mode = "thread"
        self.max_workers = 1
        self._executor = None
        self._executor_lock = threading.Lock()
        self._executor_mode = None
        self._executor_workers = 0

        # Independent executor for single runs (bypasses max_workers)
        self._independent_executor = None

        self._running_ids: set = set()

        # Startup
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

        import atexit
        atexit.register(self._cleanup_on_shutdown)

    def _cleanup_on_shutdown(self):
        """Cleanly fail any pending/running tasks when the application exits."""
        logger.info("System shutting down — cleaning up stuck task states...")
        # Use timeout to avoid hard deadlock if crashed mid-lock
        acquired = self._lock.acquire(timeout=2.0)
        try:
            for t in self.tasks:
                if not t:
                    continue
                if t["status"] in ("running", "queued"):
                    t["status"] = "failed"
                    now_str = get_now_str()
                    self._mark_failed_on_disk(t)
                    
                    # Log the forceful termination to error.log
                    log_dir = os.path.join(t["dir"], RUN_LOGS_DIR)
                    os.makedirs(log_dir, exist_ok=True)
                    error_log = os.path.join(log_dir, ERROR_LOG_FILENAME)
                    try:
                        with open(error_log, "a", encoding="utf-8") as f:
                            f.write(f"\n[{now_str}] Task forcibly terminated due to system shutdown or Ctrl+C.\n")
                    except Exception as e:
                        logger.error("Failed to write error.log for %s: %s", t['name'], e)
        finally:
            if acquired:
                self._lock.release()

    # ──────────────────────────────────────────────────────────
    #  Disk scanning
    # ──────────────────────────────────────────────────────────

    def on_change(self, callback: Callable[[], None]) -> None:
        """Register a callback for when task state changes (reactive UI)."""
        with self._observer_lock:
            if callback not in self._observers:
                self._observers.append(callback)

    def off_change(self, callback: Callable[[], None]) -> None:
        """Unregister a previously registered callback."""
        with self._observer_lock:
            if callback in self._observers:
                self._observers.remove(callback)

    def trigger_update(self) -> None:
        """Notify all registered observers."""
        with self._observer_lock:
            callbacks = list(self._observers)

        for cb in callbacks:
            try:
                cb()
            except Exception as e:
                logger.error("Observer callback error: %s", e)

    def scan_disk_async(self) -> None:
        """Run scan_disk in a background thread and trigger updates when done."""
        def _job():
            self.scan_disk()
            self.trigger_update()
        threading.Thread(target=_job, daemon=True).start()

    def scan_disk(self) -> None:
        """Full scan of *root_dir* — rebuild the in-memory task list."""
        if not self.tasks_dir or not os.path.exists(self.tasks_dir):
            logger.warning("root_dir does not exist: %s", self.tasks_dir)
            with self._lock:
                self.tasks = []
            return

        subdirs = sorted(
            (d for d in os.listdir(self.tasks_dir)
             if os.path.isdir(os.path.join(self.tasks_dir, d))
             and d != TRASH_DIR),
            key=lambda x: os.path.getmtime(
                os.path.join(self.tasks_dir, x)
            ),
            reverse=True,
        )

        new_tasks = []
        for d in subdirs:
            task = self._load_task_dir(d)
            if task is not None:
                new_tasks.append(task)

        with self._lock:
            self.tasks = new_tasks
        logger.debug("scan_disk completed: %d tasks found", len(self.tasks))

    def _load_task_dir(self, dir_name: str) -> Dict[str, Any] | None:
        """Parse a single task directory into a task dict (or ``None``)."""
        task_dir = os.path.join(self.tasks_dir, dir_name)
        info_path = os.path.join(task_dir, TASK_INFO_FILENAME)

        if not os.path.exists(info_path):
            return None

        cfg_path = next(
            (p for p in (
                os.path.join(task_dir, CONFIG_FILENAME),
                os.path.join(task_dir, "parameters.yaml"),
            ) if os.path.exists(p)),
            None,
        )
        if not cfg_path:
            return None

        try:
            info = load_task_info(task_dir)
            if not info:
                return None
        except Exception as exc:
            logger.error("Error loading info for %s: %s", dir_name, exc)
            return None

        # Mark orphan "running" tasks as failed
        task_name = info.get("name")
        if info.get("status") == "running" and task_name not in self._running_ids:
            pid = self._latest_pid(info)
            if not (pid and is_pid_running(pid)):
                self._mark_failed_on_disk({"dir": task_dir})
                info["status"] = "failed"
                logger.warning("%s: running but process gone — marked FAILED", dir_name)

        try:
            config_data = load_yaml(cfg_path)
        except Exception as exc:
            logger.error("Error loading config for %s: %s", dir_name, exc)
            return None

        task = {
            "dir": task_dir.replace("\\", "/"),
            "name": info.get("name", dir_name),
            "status": info.get("status", "unknown"),
            "created_at": info.get("created_at"),
            "config": config_data,
            "log": "",
            "progress": info.get("progress", 0.0),
            "env": info.get("env", {}),
            "pinned": info.get("pinned", False),
            "script": info.get("script"),
            "start_times": info.get("start_times", []),
            "finish_times": info.get("finish_times", []),
            "pids": info.get("pids", []),
            "records": len(info.get("records", [])),
            "_mtime": os.path.getmtime(info_path),
        }
        pending_run_index = info.get("run_index", info.get("_run_index"))
        if pending_run_index:
            task["run_index"] = pending_run_index
        return task

    def refresh_from_disk(
        self,
        task_ids: List[str] = None,
        force_all: bool = False,
        check_all: bool = False,
    ) -> bool:
        """
        Refresh in-memory task states from disk.
        Returns True if any task state was actually modified.
        """
        has_changed = False
        with self._lock:
            # Copy list to avoid modification during iteration
            current = list(self.tasks)

        target_ids = set(task_ids) if task_ids else None

        for t in current:
            if not t:
                continue
            # Decide whether to refresh this task
            if not (force_all or check_all or (target_ids and t["name"] in target_ids) or t["status"] in ("running", "queued")):
                continue

            info_path = os.path.join(t["dir"], TASK_INFO_FILENAME)
            if not os.path.exists(info_path):
                # If folder gone, strictly speaking we might want to remove it,
                # but for now just skip updating.
                continue

            try:
                mtime = os.path.getmtime(info_path)
                if not force_all and t.get("_mtime") == mtime:
                    continue
                info = load_task_info(t["dir"])
                t["_mtime"] = mtime
                t.update({
                    "status": info.get("status", t["status"]),
                    "progress": info.get("progress", t.get("progress", 0.0)),
                    "start_times": info.get("start_times", []),
                    "finish_times": info.get("finish_times", []),
                    "pids": info.get("pids", []),
                    "records": len(info.get("records", []))
                })
                has_changed = True
            except Exception:
                pass
        
        return has_changed


    # ──────────────────────────────────────────────────────────
    #  CRUD
    # ──────────────────────────────────────────────────────────

    def add_task(self, task_obj: Dict[str, Any]) -> None:
        with self._lock:
            self.tasks.insert(0, task_obj)
        self.trigger_update()

    def add_tasks(self, task_objs: List[Dict[str, Any]]) -> None:
        with self._lock:
            for task in reversed(task_objs):
                self.tasks.insert(0, task)
        self.trigger_update()

    def start_batch_tasks(
        self,
        task_ids: List[str],
        execution_mode: str = None,
        max_workers: int = None,
    ) -> None:
        if execution_mode:
            self.execution_mode = execution_mode
        if max_workers:
            self.max_workers = max_workers

        # Synchronously set status to "queued" so the UI reflects immediately
        to_sync = []
        with self._lock:
            for t in self.tasks:
                if not t:
                    continue
                if t["name"] in task_ids:
                    t["status"] = "queued"
                    starts = t.get("start_times", [])
                    t["run_index"] = len(starts) + 1
                    to_sync.append((t, t["run_index"]))

        self.is_processing = True
        self.trigger_update()

        # Disk serialization in background thread (slow IO)
        def _job():
            logger.info("Queued %d task(s) for execution. Processing IO...", len(task_ids))
            for t, run_idx in to_sync:
                self._sync_status_to_disk(t, "queued", run_index=run_idx)

        threading.Thread(target=_job, daemon=True).start()

    def start_task_now(
        self,
        task_id: str,
        execution_mode: str = None,
    ) -> None:
        """Immediately submit a single task to execution independently of the batch queue."""
        if not execution_mode:
            execution_mode = self.execution_mode

        target = None
        run_index = 1
        with self._lock:
            for t in self.tasks:
                if not t:
                    continue
                if t["name"] == task_id:
                    t["status"] = "running"
                    self._clean_aborted_runs(t)
                    starts = t.get("start_times", [])
                    run_index = len(starts) + 1
                    t["run_index"] = run_index
                    self._running_ids.add(t["name"])
                    target = t
                    break

        if not target:
            return

        self.trigger_update()

        # Submit to independent executor
        try:
            with self._executor_lock:
                if not self._independent_executor:
                    cls = ProcessPoolExecutor if execution_mode == "process" else ThreadPoolExecutor
                    # Single-run executor doesn't strictly limit workers, but let's give it a high cap
                    self._independent_executor = cls(max_workers=32)

            future = self._independent_executor.submit(
                run_task_worker,
                target["dir"], target["name"],
                target["created_at"], target["config"],
                target.get("env", {}), run_index,
            )
            logger.debug("Submitted task %s to independent executor", target["name"])
            future.add_done_callback(
                lambda f, tid=target["name"]: self._on_task_done(f, tid)
            )
        except Exception as exc:
            with self._lock:
                self._running_ids.discard(target["name"])
                target["status"] = "failed"
                self._mark_failed_on_disk(target)
            logger.error("Failed to submit task %s: %s", target["name"], exc)
            
        # Disk IO in background
        def _job():
            self._sync_status_to_disk(target, "running", run_index=run_index)
        threading.Thread(target=_job, daemon=True).start()

    def rerun_task(self, task_id: str) -> bool:
        """Run again a completed/failed task."""
        with self._lock:
            target = self._find(task_id)
            if not target or target["status"] not in ("completed", "failed"):
                return False

            self._clean_aborted_runs(target)
            starts = target.get("start_times", [])
            run_index = len(starts) + 1
            target["status"] = "queued"
            target["run_index"] = run_index
            self._sync_status_to_disk(
                target, "queued", run_index=run_index,
            )

        self.is_processing = True
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
            logger.info("Cancelled task %s", task_id[:8])
            self.trigger_update()
            return True

    def delete_tasks(self, task_ids: List[str]) -> None:
        """Bulk soft-delete: move multiple task folders to ``.trash`` with a single UI trigger."""
        targets = []
        with self._lock:
            for tid in task_ids:
                target = self._find(tid)
                if target:
                    # Cancel the task if it is running or queued
                    if target["status"] in ("running", "queued"):
                        if target["status"] == "running":
                            pid = self._latest_pid_from_disk(target)
                            if pid:
                                kill_process(int(pid))
                            self._running_ids.discard(tid)
                        
                        target["status"] = "failed"
                        self._mark_failed_on_disk(target)
                    
                    self.tasks.remove(target)
                    targets.append(target)
        
        if not targets:
            return
            
        self.trigger_update()

        # Retry logic for Windows file locking
        trash_dir = os.path.join(self.tasks_dir, TRASH_DIR)
        os.makedirs(trash_dir, exist_ok=True)
        
        for target in targets:
            max_retries = 3
            for i in range(max_retries):
                try:
                    folder = os.path.basename(target["dir"])
                    dest = os.path.join(trash_dir, folder)
                    if os.path.exists(dest):
                        ts = get_now_str()
                        dest = os.path.join(trash_dir, f"{folder}_{ts}")
                    shutil.move(target["dir"], dest)
                    break
                except Exception as exc:
                    if i < max_retries - 1:
                        time.sleep(0.2)
                    else:
                        logger.error("Error moving task to trash after retries: %s", exc)
                        try:
                            shutil.rmtree(target["dir"])
                        except Exception:
                            pass


    # ──────────────────────────────────────────────────────────
    #  Scheduler
    # ──────────────────────────────────────────────────────────

    def _scheduler_loop(self) -> None:
        _last_trigger = 0.0  # throttle UI notifications to max 1/sec
        while True:
            try:
                # Top of the loop: Check for changes to running/queued tasks
                if self.refresh_from_disk():
                    now = time.time()
                    if now - _last_trigger >= 1.0:
                        _last_trigger = now
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
                    time.sleep(0.1)
                    continue

                try:
                    future = self._executor.submit(
                        run_task_worker,
                        target["dir"], target["name"],
                        target["created_at"], target["config"],
                        target.get("env", {}), run_index,
                    )
                    logger.debug("Submitted task %s to executor (running=%d/%d)", 
                                 target["name"], len(self._running_ids), self.max_workers)
                    future.add_done_callback(
                        lambda f, tid=target["name"]: self._on_task_done(f, tid)
                    )
                except Exception as exc:
                    # CRITICAL: If submit fails, we must remove the ID from running_ids
                    # or it will block a slot forever.
                    with self._lock:
                        self._running_ids.discard(target["name"])
                        target["status"] = "failed"
                        self._mark_failed_on_disk(target)
                        
                    logger.error("Failed to submit task %s: %s", target["name"], exc)

                    
            except Exception as exc:
                logger.error("Scheduler error: %s", exc, exc_info=True)
                time.sleep(1)

            time.sleep(0.2)

    def _ensure_executor(self) -> None:
        """Lazily create / recreate the executor pool when settings change."""
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

            cls = (ProcessPoolExecutor
                   if self.execution_mode == "process"
                   else ThreadPoolExecutor)
            self._executor = cls(max_workers=workers)
            self._executor_mode = self.execution_mode
            self._executor_workers = workers

    def _pick_queued_task(self):
        """Pick the next queued task and mark it running (thread-safe)."""
        with self._lock:
            for t in self.tasks:
                if not t:
                    continue
                if t["status"] == "queued":
                    run_index = t.pop("run_index", t.pop("_run_index", 1))
                    t["status"] = "running"
                    self._running_ids.add(t["name"])
                    return t, run_index
        return None, 1

    def _on_task_done(self, future: Future, task_id: str) -> None:
        worker_error = None
        try:
            exc = future.exception()
            if exc:
                worker_error = exc
                logger.error(
                    "Worker for %s raised: %s", task_id, exc,
                )
        except Exception:
            pass

        with self._lock:
            self._running_ids.discard(task_id)
            t = self._find(task_id)
            if not t:
                return

            try:
                info = load_task_info(t["dir"])
                # Minimal info to keep UI in sync
                t.update({
                    "status": info.get("status", "completed"),
                    "progress": info.get("progress", 1.0),
                    "start_times": info.get("start_times", []),
                    "finish_times": info.get("finish_times", []),
                    "pids": info.get("pids", []),
                    "records": len(info.get("records", []))
                })
            except Exception:
                pass

            if worker_error and t["status"] in ("running", "queued"):
                t["status"] = "failed"
                disk_info = load_task_info(t["dir"])
                disk_info["status"] = "failed"
                save_task_info(t["dir"], disk_info)
        
        # We just caught a finish or failure, notify observers
        self.trigger_update()

    # ──────────────────────────────────────────────────────────
    #  Internal helpers
    # ──────────────────────────────────────────────────────────

    def _find(self, task_id: str):
        """Find a task by ID (caller must hold _lock if writing)."""
        return next((t for t in self.tasks if t["name"] == task_id), None)

    @staticmethod
    def _latest_pid(info: dict):
        """Return the most relevant PID from an info dict."""
        pids = info.get("pids", [])
        return pids[-1] if isinstance(pids, list) and pids else None

    def _latest_pid_from_disk(self, task: dict):
        task_info = load_task_info(task["dir"])
        return self._latest_pid(task_info) if task_info else None

    def _clean_aborted_runs(self, task: dict):
        task_info = load_task_info(task["dir"])
        if not task_info: return
        valid_sz = normalize_run_history(task_info)

        from pyruns._config import RUN_LOGS_DIR
        log_dir = os.path.join(task["dir"], RUN_LOGS_DIR)
        if os.path.exists(log_dir):
            i = valid_sz + 1
            while True:
                bad_log = os.path.join(log_dir, f"run{i}.log")
                if os.path.exists(bad_log):
                    try:
                        os.remove(bad_log)
                    except OSError:
                        pass
                    i += 1
                else:
                    break

        save_task_info(task["dir"], task_info)
        task.update({
            "start_times": task_info.get("start_times", []),
            "finish_times": task_info.get("finish_times", []),
            "pids": task_info.get("pids", []),
            "run_index": task_info.get("run_index", valid_sz),
        })

    def _sync_status_to_disk(
        self, task: dict, status: str, run_index: int = 1,
    ) -> None:
        task_info = load_task_info(task["dir"])
        if not task_info:
            return
        task_info["status"] = status
        task_info["run_index"] = run_index
        task_info.pop("_run_index", None)
        save_task_info(task["dir"], task_info)

    def _mark_failed_on_disk(self, task: dict) -> None:
        task_info = load_task_info(task["dir"])
        if not task_info:
            return
        task_info["status"] = "failed"
        normalize_run_history(task_info)
        save_task_info(task["dir"], task_info)
        task.update({
            "status": "failed",
            "start_times": task_info.get("start_times", []),
            "finish_times": task_info.get("finish_times", []),
            "pids": task_info.get("pids", []),
            "records": len(task_info.get("records", [])),
            "run_index": task_info.get("run_index", 0),
        })
    # _normalize_run_history has been moved to utils/info_io.py → normalize_run_history
