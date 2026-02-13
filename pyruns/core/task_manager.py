import os
import json
import time
import threading
import shutil
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, Future

from pyruns._config import ROOT_DIR, INFO_FILENAME, CONFIG_FILENAME, LOG_FILENAME, RERUN_LOG_DIR, TRASH_DIR
from pyruns.utils.config_utils import load_yaml
from pyruns.core.executor import run_task_worker
from pyruns.utils import get_logger

logger = get_logger(__name__)

class TaskManager:
    def __init__(self, root_dir: str = ROOT_DIR):
        self.root_dir = root_dir
        self.tasks: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        
        # Execution State
        self.is_processing = False
        self.execution_mode = "thread"
        self.max_workers = 1
        
        # Executors
        self._executor = None
        self._executor_mode = None
        self._executor_workers = None
        self._executor_lock = threading.Lock()
        self._running_ids = set()

        # Startup
        self.scan_disk()
        threading.Thread(target=self._scheduler_loop, daemon=True).start()

    # ─── Disk Scanning ───────────────────────────────────────────

    def scan_disk(self) -> None:
        with self._lock:
            self.tasks = []

            if not self.root_dir or not os.path.exists(self.root_dir):
                logger.warning(f"[TaskManager] root_dir doesn't exist: {self.root_dir}")
                return

            subdirs = [
                d for d in os.listdir(self.root_dir)
                if os.path.isdir(os.path.join(self.root_dir, d)) and d != TRASH_DIR
            ]
            subdirs.sort(key=lambda x: os.path.getmtime(os.path.join(self.root_dir, x)), reverse=True)

            for d in subdirs:
                task_dir = os.path.join(self.root_dir, d)
                info_path = os.path.join(task_dir, INFO_FILENAME)
                
                # 1. Check task_info.json
                if not os.path.exists(info_path):
                    logger.warning(f"[TaskManager] {d}: Missing task_info.json")
                    continue
                
                # 2. Check config.yaml
                cfg_paths = [os.path.join(task_dir, f) for f in [CONFIG_FILENAME, "parameters.yaml"]]
                cfg_path = next((p for p in cfg_paths if os.path.exists(p)), None)
                if not cfg_path:
                    logger.warning(f"[TaskManager] {d}: Missing config.yaml")
                    continue
                
                # 3. Load info
                try:
                    with open(info_path, "r", encoding="utf-8") as f:
                        info = json.load(f)
                except Exception as e:
                    logger.error(f"[TaskManager] Error loading info for {d}: {e}")
                    continue

                # 4. Check run.log (optional — don't skip tasks just because log is missing)
                log_path = os.path.join(task_dir, LOG_FILENAME)

                try:
                    config_data = load_yaml(cfg_path)

                    # 只有不在当前执行器中的 "running" 任务才认为是崩溃残留
                    task_id = info.get("id")
                    if info.get("status") == "running" and task_id not in self._running_ids:
                        # 检查 PID 是否还存活
                        pid = info.get("run_pid")
                        # 也检查 rerun_pid 列表中最后一个
                        rerun_pids = info.get("rerun_pid", [])
                        if rerun_pids and rerun_pids[-1]:
                            pid = rerun_pids[-1]
                        if pid and self._is_pid_running(pid):
                            pass  # 进程仍在运行
                        else:
                            info["status"] = "failed"
                            info["run_pid"] = None
                            with open(info_path, "w", encoding="utf-8") as f:
                                json.dump(info, f, indent=4)
                            logger.warning(f"[TaskManager] {d}: running but process not found, marked FAILED.")

                    # Construct task object (log content loaded lazily in dialog)
                    task = {
                        "id": info.get("id"),
                        "dir": task_dir,
                        "name": info.get("name", d),
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
                    }
                    # Restore persisted _rerun_index (for scan_disk race safety)
                    if "_rerun_index" in info:
                        task["_rerun_index"] = info["_rerun_index"]
                    self.tasks.append(task)
                    
                except Exception as e:
                    logger.error(f"[TaskManager] Error loading {d}: {e}")
                    continue

    @staticmethod
    def _is_pid_running(pid) -> bool:
        """检查 PID 是否仍在运行 (跨平台)。"""
        if not pid:
            return False
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False

        if os.name == "nt":
            # Windows: signal 0 不支持，使用 ctypes 检查进程句柄
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                SYNCHRONIZE = 0x00100000
                handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
                if handle:
                    kernel32.CloseHandle(handle)
                    return True
                return False
            except Exception:
                return False
        else:
            # Unix: signal 0 检查进程是否存在
            try:
                os.kill(pid, 0)
                return True
            except (OSError, ProcessLookupError):
                return False

    @staticmethod
    def _kill_process(pid: int) -> None:
        """跨平台终止进程。"""
        try:
            if os.name == "nt":
                # Windows: taskkill /F 强制终止进程树
                import subprocess as _sp
                _sp.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=5,
                )
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
        except Exception as e:
            logger.warning(f"[TaskManager] Failed to kill PID {pid}: {e}")

    def refresh_from_disk(self) -> None:
        """Reload status/progress from disk — only for active (running/queued) tasks."""
        with self._lock:
            current_tasks = list(self.tasks)

        for t in current_tasks:
            # 只刷新活跃状态的任务；已完成/失败/取消的不会自己改变
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
                t["rerun_pid"] = info.get("rerun_pid", t.get("rerun_pid", []))
            except Exception:
                pass

    # ─── Task CRUD ───────────────────────────────────────────────

    def add_task(self, task_obj: Dict[str, Any]) -> None:
        with self._lock:
            self.tasks.insert(0, task_obj)

    def add_tasks(self, task_objs: List[Dict[str, Any]]) -> None:
        with self._lock:
            for task in reversed(task_objs):
                self.tasks.insert(0, task)

    def start_batch_tasks(self, task_ids: List[str], execution_mode: str = None, max_workers: int = None) -> None:
        if execution_mode: self.execution_mode = execution_mode
        if max_workers: self.max_workers = max_workers
        
        with self._lock:
            for t in self.tasks:
                if t["id"] in task_ids:
                    t["status"] = "queued"
                    t["_rerun_index"] = 0  # 标记为首次运行
                    # Sync to disk (persist _rerun_index to survive scan_disk race)
                    info_path = os.path.join(t["dir"], INFO_FILENAME)
                    if os.path.exists(info_path):
                        try:
                            with open(info_path, "r", encoding="utf-8") as f:
                                d = json.load(f)
                            d["status"] = "queued"
                            d["_rerun_index"] = 0
                            with open(info_path, "w", encoding="utf-8") as f:
                                json.dump(d, f, indent=4, ensure_ascii=False)
                        except Exception as e:
                            logger.error(f"[TaskManager] Failed to sync queued status: {e}")
        
        self.is_processing = True

    def rerun_task(self, task_id: str) -> bool:
        """重跑一个已完成/失败/取消的任务。"""
        with self._lock:
            target = next((t for t in self.tasks if t["id"] == task_id), None)
            if not target:
                return False
            if target["status"] not in ("completed", "failed"):
                return False

            # 计算 rerun 序号 — 基于已有 rerun_at 列表
            rerun_at_list = target.get("rerun_at", [])
            rerun_index = len(rerun_at_list) + 1

            target["status"] = "queued"
            target["_rerun_index"] = rerun_index

            # Sync to disk (persist _rerun_index to survive scan_disk race)
            info_path = os.path.join(target["dir"], INFO_FILENAME)
            if os.path.exists(info_path):
                try:
                    with open(info_path, "r", encoding="utf-8") as f:
                        d = json.load(f)
                    d["status"] = "queued"
                    d["_rerun_index"] = rerun_index
                    with open(info_path, "w", encoding="utf-8") as f:
                        json.dump(d, f, indent=4, ensure_ascii=False)
                except Exception as e:
                    logger.error(f"[TaskManager] Failed to sync rerun queued status: {e}")

        self.is_processing = True
        return True

    def cancel_task(self, task_id: str) -> bool:
        """取消一个 queued 或 running 的任务。"""
        with self._lock:
            target = next((t for t in self.tasks if t["id"] == task_id), None)
            if not target:
                return False

            old_status = target["status"]
            if old_status not in ("queued", "running"):
                return False

            # 如果正在运行，尝试杀掉子进程
            if old_status == "running":
                info_path = os.path.join(target["dir"], INFO_FILENAME)
                pid = None
                try:
                    with open(info_path, "r", encoding="utf-8") as f:
                        info = json.load(f)
                    # 优先使用 rerun_pid 最后一项，否则 run_pid
                    rerun_pids = info.get("rerun_pid", [])
                    if rerun_pids and rerun_pids[-1]:
                        pid = rerun_pids[-1]
                    else:
                        pid = info.get("run_pid")
                except Exception:
                    pass

                if pid:
                    self._kill_process(int(pid))

                self._running_ids.discard(task_id)

            # 更新状态 → failed (不再有 canceled 状态)
            target["status"] = "failed"
            info_path = os.path.join(target["dir"], INFO_FILENAME)
            try:
                with open(info_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                d["status"] = "failed"
                d["run_pid"] = None
                # 清除 rerun_pid 最后一个活跃的 PID
                rp = d.get("rerun_pid", [])
                if rp and rp[-1] is not None:
                    rp[-1] = None
                    d["rerun_pid"] = rp
                with open(info_path, "w", encoding="utf-8") as f:
                    json.dump(d, f, indent=4)
            except Exception:
                pass

            return True

    def delete_task(self, task_id: str) -> None:
        """软删除: 将任务文件夹移到 .trash 目录而非真删除。"""
        with self._lock:
            target = next((t for t in self.tasks if t["id"] == task_id), None)
            if target:
                self.tasks.remove(target)
                try:
                    trash_dir = os.path.join(self.root_dir, TRASH_DIR)
                    os.makedirs(trash_dir, exist_ok=True)
                    folder_name = os.path.basename(target["dir"])
                    dest = os.path.join(trash_dir, folder_name)
                    # 同名冲突: 追加时间戳
                    if os.path.exists(dest):
                        import datetime
                        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                        dest = os.path.join(trash_dir, f"{folder_name}_{ts}")
                    shutil.move(target["dir"], dest)
                except Exception as e:
                    logger.error(f"[TaskManager] Error moving task to trash: {e}")
                    # 兜底: 如果 move 失败, 尝试真删除
                    try:
                        shutil.rmtree(target["dir"])
                    except Exception:
                        pass

    # ─── Scheduler Logic ─────────────────────────────────────────

    def _ensure_executor(self):
        """确保执行器已初始化，按需切换 Thread / Process 模式。

        Thread 模式（推荐）: 因为实际计算在子进程 (subprocess) 中执行，
            工作线程只负责 `proc.wait()`，开销极小。
        Process 模式: 将 run_task_worker 放到独立进程中执行，
            需要函数参数可序列化 (pickle)。
        """
        with self._executor_lock:
            workers = max(1, int(self.max_workers))
            changes = (self._executor_mode != self.execution_mode) or (self._executor_workers != workers)

            if self._executor and not changes:
                return

            if self._executor:
                try:
                    self._executor.shutdown(wait=False)
                except Exception:
                    pass

            if self.execution_mode == "process":
                self._executor = ProcessPoolExecutor(max_workers=workers)
            else:
                self._executor = ThreadPoolExecutor(max_workers=workers)

            self._executor_mode = self.execution_mode
            self._executor_workers = workers

    def _scheduler_loop(self):
        while True:
            try:
                if not self.is_processing:
                    time.sleep(0.1)
                    continue

                self._ensure_executor()

                current_running = len(self._running_ids)
                if current_running >= self.max_workers:
                    time.sleep(0.1)
                    continue

                # Pick Task & mark as running (inside lock to prevent race with cancel)
                target_task = None
                rerun_index = 0
                with self._lock:
                    for t in self.tasks:
                        if t["status"] == "queued":
                            target_task = t
                            break
                    if target_task:
                        rerun_index = target_task.pop("_rerun_index", 0)
                        target_task["status"] = "running"
                        self._running_ids.add(target_task["id"])

                if not target_task:
                    time.sleep(0.2)
                    continue

                # Launch (GPU 等设置通过 env vars 传入，无需单独分配)
                try:
                    future = self._executor.submit(
                        run_task_worker,
                        target_task["dir"],
                        target_task["id"],
                        target_task["name"],
                        target_task["created_at"],
                        target_task["config"],
                        target_task.get("env", {}),
                        rerun_index,
                    )
                    future.add_done_callback(
                        lambda f, tid=target_task["id"]: self._on_task_done(f, tid)
                    )
                except Exception as submit_err:
                    logger.error(f"[Scheduler] Failed to submit task {target_task['name']}: {submit_err}")
                    with self._lock:
                        target_task["status"] = "failed"
                        self._running_ids.discard(target_task["id"])

                time.sleep(0.1)

            except Exception as loop_err:
                logger.error(f"[Scheduler] Unexpected error in scheduler loop: {loop_err}", exc_info=True)
                time.sleep(1)  # Avoid tight error loop

    def _on_task_done(self, future: Future, task_id: str):

        # Check if the worker raised an unhandled exception
        worker_error = None
        try:
            exc = future.exception()
            if exc:
                worker_error = exc
                logger.error(f"[Scheduler] Worker for task {task_id} raised exception: {exc}")
        except Exception:
            pass

        with self._lock:
            self._running_ids.discard(task_id)
            t = next((x for x in self.tasks if x["id"] == task_id), None)
            if t:
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
                except Exception:
                    pass

                # If worker crashed without updating status on disk, force 'failed'
                if worker_error and t["status"] in ("running", "queued"):
                    t["status"] = "failed"
                    try:
                        with open(info_path, "r", encoding="utf-8") as f:
                            d = json.load(f)
                        d["status"] = "failed"
                        d["run_pid"] = None
                        with open(info_path, "w", encoding="utf-8") as f:
                            json.dump(d, f, indent=4, ensure_ascii=False)
                    except Exception:
                        pass

