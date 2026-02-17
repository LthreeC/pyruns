"""
Task Manager — scans disk, manages task lifecycle, schedules execution.

Responsibilities:
  • ``scan_disk()``         – full directory scan, build in-memory task list
  • ``refresh_from_disk()`` – lightweight status update for active tasks only
  • CRUD operations         – add / start / rerun / cancel / delete
  • Scheduler loop          – picks queued tasks and submits them to an executor
"""
import os
import json
import time
import threading
import shutil
from typing import List, Dict, Any
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, Future

from pyruns._config import (
    ROOT_DIR, INFO_FILENAME, CONFIG_FILENAME,
    LOG_FILENAME, RERUN_LOG_DIR, TRASH_DIR, MONITOR_KEY,
)
from pyruns.utils.config_utils import load_yaml
from pyruns.utils.process_utils import is_pid_running, kill_process
from pyruns.core.executor import run_task_worker
from pyruns.utils import get_logger

logger = get_logger(__name__)


class TaskManager:
    """Central task registry and scheduler.

    Parameters
    ----------
    root_dir : str
        Directory that contains task sub-folders (each with ``task_info.json``).
    """

    def __init__(self, root_dir: str = ROOT_DIR):
        self.root_dir = root_dir
        self.tasks: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

        # Execution state
        self.is_processing = False
        self.execution_mode = "thread"
        self.max_workers = 1

        # Executor pool (created lazily)
        self._executor = None
        self._executor_mode = None
        self._executor_workers = None
        self._executor_lock = threading.Lock()
        self._running_ids: set = set()

        # Startup
        self.scan_disk()
        logger.info("TaskManager initialised  root=%s  tasks=%d",
                    root_dir, len(self.tasks))
        threading.Thread(target=self._scheduler_loop, daemon=True).start()

    # ──────────────────────────────────────────────────────────
    #  Disk scanning
    # ──────────────────────────────────────────────────────────

    def scan_disk(self) -> None:
        """Full scan of *root_dir* — rebuild the in-memory task list."""
        with self._lock:
            self.tasks = []

            if not self.root_dir or not os.path.exists(self.root_dir):
                logger.warning("root_dir does not exist: %s", self.root_dir)
                return

            subdirs = sorted(
                (d for d in os.listdir(self.root_dir)
                 if os.path.isdir(os.path.join(self.root_dir, d))
                 and d != TRASH_DIR),
                key=lambda x: os.path.getmtime(
                    os.path.join(self.root_dir, x)
                ),
                reverse=True,
            )

            for d in subdirs:
                task = self._load_task_dir(d)
                if task is not None:
                    self.tasks.append(task)

            logger.debug("scan_disk completed: %d tasks found", len(self.tasks))

    def _load_task_dir(self, dir_name: str) -> Dict[str, Any] | None:
        """Parse a single task directory into a task dict (or ``None``)."""
        task_dir = os.path.join(self.root_dir, dir_name)
        info_path = os.path.join(task_dir, INFO_FILENAME)

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
            with open(info_path, "r", encoding="utf-8") as f:
                info = json.load(f)
        except Exception as exc:
            logger.error("Error loading info for %s: %s", dir_name, exc)
            return None

        # Mark orphan "running" tasks as failed
        task_id = info.get("id")
        if (
            info.get("status") == "running"
            and task_id not in self._running_ids
        ):
            pid = self._latest_pid(info)
            if not (pid and is_pid_running(pid)):
                info["status"] = "failed"
                info["run_pid"] = None
                self._write_info(info_path, info)
                logger.warning(
                    "%s: running but process gone — marked FAILED", dir_name,
                )

        try:
            config_data = load_yaml(cfg_path)
        except Exception as exc:
            logger.error("Error loading config for %s: %s", dir_name, exc)
            return None

        task = {
            "id": task_id,
            "dir": task_dir,
            "name": info.get("name", dir_name),
            "status": info.get("status", "unknown"),
            "created_at": info.get("created_at"),
            "config": config_data,
            "log": "",
            "progress": info.get("progress", 0.0),
            "env": info.get("env", {}),
            "pinned": info.get("pinned", False),
            "script": info.get("script"),
            "run_at": info.get("run_at"),
            "rerun_at": info.get("rerun_at", []),
            "run_pid": info.get("run_pid"),
            "rerun_pid": info.get("rerun_pid", []),
            "monitor_count": len(info.get(MONITOR_KEY, [])),
        }
        if "_rerun_index" in info:
            task["_rerun_index"] = info["_rerun_index"]
        return task

    def refresh_from_disk(self) -> None:
        """Lightweight update — only re-read running/queued tasks."""
        with self._lock:
            current = list(self.tasks)

        for t in current:
            if t["status"] not in ("running", "queued"):
                continue
            info_path = os.path.join(t["dir"], INFO_FILENAME)
            if not os.path.exists(info_path):
                continue
            try:
                with open(info_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
                t["status"] = info.get("status", t["status"])
                t["progress"] = info.get("progress", t.get("progress", 0.0))
                t["run_at"] = info.get("run_at", t.get("run_at"))
                t["rerun_at"] = info.get("rerun_at", t.get("rerun_at", []))
                t["run_pid"] = info.get("run_pid", t.get("run_pid"))
                t["rerun_pid"] = info.get(
                    "rerun_pid", t.get("rerun_pid", [])
                )
                t["monitor_count"] = len(info.get(MONITOR_KEY, []))
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────
    #  CRUD
    # ──────────────────────────────────────────────────────────

    def add_task(self, task_obj: Dict[str, Any]) -> None:
        with self._lock:
            self.tasks.insert(0, task_obj)

    def add_tasks(self, task_objs: List[Dict[str, Any]]) -> None:
        with self._lock:
            for task in reversed(task_objs):
                self.tasks.insert(0, task)

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

        with self._lock:
            for t in self.tasks:
                if t["id"] in task_ids:
                    t["status"] = "queued"
                    t["_rerun_index"] = 0
                    self._sync_status_to_disk(t, "queued", rerun_index=0)

        self.is_processing = True
        logger.info("Queued %d task(s) for execution", len(task_ids))

    def rerun_task(self, task_id: str) -> bool:
        """Re-run a completed/failed task."""
        with self._lock:
            target = self._find(task_id)
            if not target or target["status"] not in ("completed", "failed"):
                return False

            rerun_index = len(target.get("rerun_at", [])) + 1
            target["status"] = "queued"
            target["_rerun_index"] = rerun_index
            self._sync_status_to_disk(
                target, "queued", rerun_index=rerun_index,
            )

        self.is_processing = True
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
            return True

    def delete_task(self, task_id: str) -> None:
        """Soft-delete: move task folder to ``.trash``."""
        with self._lock:
            target = self._find(task_id)
            if not target:
                return
            self.tasks.remove(target)

        try:
            trash_dir = os.path.join(self.root_dir, TRASH_DIR)
            os.makedirs(trash_dir, exist_ok=True)
            folder = os.path.basename(target["dir"])
            dest = os.path.join(trash_dir, folder)
            if os.path.exists(dest):
                import datetime
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                dest = os.path.join(trash_dir, f"{folder}_{ts}")
            shutil.move(target["dir"], dest)
        except Exception as exc:
            logger.error("Error moving task to trash: %s", exc)
            try:
                shutil.rmtree(target["dir"])
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────
    #  Scheduler
    # ──────────────────────────────────────────────────────────

    def _scheduler_loop(self) -> None:
        while True:
            try:
                if not self.is_processing:
                    time.sleep(0.1)
                    continue

                self._ensure_executor()

                if len(self._running_ids) >= self.max_workers:
                    time.sleep(0.1)
                    continue

                target, rerun_index = self._pick_queued_task()
                if not target:
                    time.sleep(0.2)
                    continue

                future = self._executor.submit(
                    run_task_worker,
                    target["dir"], target["id"], target["name"],
                    target["created_at"], target["config"],
                    target.get("env", {}), rerun_index,
                )
                logger.debug("Submitted task %s to executor", target["name"])
                future.add_done_callback(
                    lambda f, tid=target["id"]: self._on_task_done(f, tid)
                )
            except Exception as exc:
                logger.error("Scheduler error: %s", exc, exc_info=True)
                time.sleep(1)

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
                if t["status"] == "queued":
                    rerun_index = t.pop("_rerun_index", 0)
                    t["status"] = "running"
                    self._running_ids.add(t["id"])
                    return t, rerun_index
        return None, 0

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

            info_path = os.path.join(t["dir"], INFO_FILENAME)
            try:
                with open(info_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
                t["status"] = info.get("status", "completed")
                t["progress"] = info.get("progress", 1.0)
                t["run_at"] = info.get("run_at", t.get("run_at"))
                t["rerun_at"] = info.get("rerun_at", t.get("rerun_at", []))
                t["run_pid"] = info.get("run_pid")
                t["rerun_pid"] = info.get("rerun_pid", [])
                t["monitor_count"] = len(info.get(MONITOR_KEY, []))
            except Exception:
                pass

            if worker_error and t["status"] in ("running", "queued"):
                t["status"] = "failed"
                self._write_info(info_path, {
                    **self._read_info(info_path),
                    "status": "failed",
                    "run_pid": None,
                })

    # ──────────────────────────────────────────────────────────
    #  Internal helpers
    # ──────────────────────────────────────────────────────────

    def _find(self, task_id: str):
        """Find a task by ID (caller must hold _lock if writing)."""
        return next((t for t in self.tasks if t["id"] == task_id), None)

    @staticmethod
    def _latest_pid(info: dict):
        """Return the most relevant PID from an info dict."""
        rerun_pids = info.get("rerun_pid", [])
        if rerun_pids and rerun_pids[-1]:
            return rerun_pids[-1]
        return info.get("run_pid")

    def _latest_pid_from_disk(self, task: dict):
        info = self._read_info(
            os.path.join(task["dir"], INFO_FILENAME)
        )
        return self._latest_pid(info) if info else None

    @staticmethod
    def _read_info(info_path: str) -> dict:
        try:
            with open(info_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @staticmethod
    def _write_info(info_path: str, data: dict) -> None:
        try:
            with open(info_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception:
            pass

    def _sync_status_to_disk(
        self, task: dict, status: str, rerun_index: int = 0,
    ) -> None:
        info_path = os.path.join(task["dir"], INFO_FILENAME)
        info = self._read_info(info_path)
        if not info:
            return
        info["status"] = status
        info["_rerun_index"] = rerun_index
        self._write_info(info_path, info)

    def _mark_failed_on_disk(self, task: dict) -> None:
        info_path = os.path.join(task["dir"], INFO_FILENAME)
        info = self._read_info(info_path)
        if not info:
            return
        info["status"] = "failed"
        info["run_pid"] = None
        rp = info.get("rerun_pid", [])
        if rp and rp[-1] is not None:
            rp[-1] = None
            info["rerun_pid"] = rp
        self._write_info(info_path, info)
