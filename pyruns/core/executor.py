import os
import sys
import json
import subprocess
import time
import datetime
from typing import Dict, Any, Optional

from pyruns._config import INFO_FILENAME, LOG_FILENAME, RERUN_LOG_DIR, ENV_CONFIG, CONFIG_FILENAME
from pyruns.core.log_io import append_log

def _update_task_info(task_dir: str, info: Dict[str, Any]) -> None:
    info_path = os.path.join(task_dir, INFO_FILENAME)
    base = {}
    if os.path.exists(info_path):
        try:
            with open(info_path, "r", encoding="utf-8") as f:
                base = json.load(f)
        except Exception:
            base = {}
    base.update(info)
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(base, f, indent=4, ensure_ascii=False)

def _prepare_env(
    extra_env: Optional[Dict[str, str]] = None,
    task_dir: Optional[str] = None,
) -> Dict[str, str]:
    env = os.environ.copy()
    # 强制子进程使用 UTF-8 输出，避免 Windows 下 GBK 编码问题
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # pyr 模式: 设置 ENV_CONFIG 让 pyruns.read() 自动找到任务的 config.yaml
    if task_dir:
        env[ENV_CONFIG] = os.path.join(task_dir, CONFIG_FILENAME)
    # GPU 等设置全部通过 env_vars 传入 (如 CUDA_VISIBLE_DEVICES)
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items() if k})
    return env

def _load_task_meta(task_dir: str) -> Dict[str, Any]:
    """从 task_info.json 读取任务元数据 (script / cmd / workdir 等)"""
    info_path = os.path.join(task_dir, INFO_FILENAME)
    try:
        with open(info_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _get_log_path(task_dir: str, rerun_index: int) -> str:
    """返回日志文件路径。rerun_index=0 表示首次运行 (run.log)，>0 表示重跑。"""
    if rerun_index <= 0:
        return os.path.join(task_dir, LOG_FILENAME)
    else:
        log_dir = os.path.join(task_dir, RERUN_LOG_DIR)
        os.makedirs(log_dir, exist_ok=True)
        return os.path.join(log_dir, f"rerun{rerun_index}.log")

def run_task_worker(
    task_dir: str,
    task_id: str,
    name: str,
    created_at: str,
    config: Dict[str, Any],
    env_vars: Optional[Dict[str, str]] = None,
    rerun_index: int = 0,
) -> Dict[str, Any]:
    """
    Worker function executed in a separate thread/process.
    config: 纯用户参数 (来自 config.yaml)
    script / cmd / workdir: 从 task_info.json 读取
    GPU 等硬件设置通过 env_vars 传入 (如 CUDA_VISIBLE_DEVICES)
    rerun_index: 0=首次运行, >0=第N次重跑
    """
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        log_path = _get_log_path(task_dir, rerun_index)
    except Exception:
        log_path = os.path.join(task_dir, LOG_FILENAME)

    # ── 从 task_info.json 读取内部元数据 ──
    task_meta = _load_task_meta(task_dir)
    script_path = task_meta.get("script")
    meta_cmd = task_meta.get("cmd")
    meta_workdir = task_meta.get("workdir")

    # 1. Update Status to Running + 记录时间
    running_info = {
        "id": task_id,
        "name": name,
        "status": "running",
        "created_at": created_at,
        "progress": 0.0,
    }
    if script_path:
        running_info["script"] = script_path

    if rerun_index <= 0:
        # 首次运行
        running_info["run_at"] = now_str
    else:
        # 重跑：追加到 rerun_at 列表
        rerun_at_list = task_meta.get("rerun_at", [])
        rerun_at_list.append(now_str)
        running_info["rerun_at"] = rerun_at_list

    _update_task_info(task_dir, running_info)

    # Clean up persisted _rerun_index (no longer needed once running)
    try:
        info_path = os.path.join(task_dir, INFO_FILENAME)
        with open(info_path, "r", encoding="utf-8") as f:
            _info = json.load(f)
        if "_rerun_index" in _info:
            del _info["_rerun_index"]
            with open(info_path, "w", encoding="utf-8") as f:
                json.dump(_info, f, indent=4, ensure_ascii=False)
    except Exception:
        pass

    run_label = f"Rerun #{rerun_index}" if rerun_index > 0 else "Run"
    append_log(log_path, f"[{now_str}] SYSTEM: {run_label} Execution Started.\n")

    # 2. Build Command
    command = meta_cmd or config.get("command")
    workdir = meta_workdir

    if not command and script_path:
        cmd_list = [sys.executable, script_path]

        for k, v in config.items():
            if isinstance(v, bool):
                if v: cmd_list.append(f"--{k}")
            elif isinstance(v, list):
                for item in v:
                    cmd_list.append(f"--{k}")
                    cmd_list.append(str(item))
            elif v is not None:
                cmd_list.append(f"--{k}")
                cmd_list.append(str(v))

        command = cmd_list
        if not workdir:
            workdir = os.path.dirname(script_path)

    # 3. Execute
    status = "failed"
    progress = 0.0
    try:
        if command:
            env = _prepare_env(env_vars, task_dir=task_dir)
            with open(log_path, "a", encoding="utf-8") as f:
                proc = subprocess.Popen(
                    command,
                    shell=isinstance(command, str),
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    cwd=workdir,
                    env=env,
                )
                # 存储 PID
                pid_info = {}
                if rerun_index <= 0:
                    pid_info["run_pid"] = proc.pid
                else:
                    meta_fresh = _load_task_meta(task_dir)
                    rerun_pid_list = meta_fresh.get("rerun_pid", [])
                    rerun_pid_list.append(proc.pid)
                    pid_info["rerun_pid"] = rerun_pid_list
                _update_task_info(task_dir, pid_info)

                ret = proc.wait()
            status = "completed" if ret == 0 else "failed"
            progress = 1.0 if ret == 0 else 0.0
        else:
            # Simulation Mode (if no script provided)
            total_steps = 30
            for i in range(total_steps):
                time.sleep(0.1)
                progress = (i + 1) / total_steps
                msg = f"Epoch {i+1}/{total_steps} | Simulated Step\n"
                append_log(log_path, msg)
                if i % 10 == 0:
                    _update_task_info(task_dir, {
                        "id": task_id, "name": name, "status": "running",
                        "created_at": created_at, "progress": progress,
                    })
            status = "completed"
            progress = 1.0

        end_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        append_log(log_path, f"[{end_str}] SYSTEM: Task {status}.\n")

        # 4. Final update: clear current PID
        final_info = {
            "id": task_id,
            "name": name,
            "status": status,
            "created_at": created_at,
            "progress": progress,
        }
        if rerun_index <= 0:
            final_info["run_pid"] = None
        else:
            # 把 rerun_pid 最后一项置为 None 表示已结束
            meta_final = _load_task_meta(task_dir)
            rp_list = meta_final.get("rerun_pid", [])
            if rp_list:
                rp_list[-1] = None
            final_info["rerun_pid"] = rp_list

        _update_task_info(task_dir, final_info)
        return {"status": status, "progress": progress}

    except Exception as e:
        append_log(log_path, f"ERROR: {str(e)}\n")
        fail_info = {
            "id": task_id,
            "name": name,
            "status": "failed",
            "created_at": created_at,
            "progress": 0.0,
        }
        if rerun_index <= 0:
            fail_info["run_pid"] = None
        else:
            meta_err = _load_task_meta(task_dir)
            rp_list = meta_err.get("rerun_pid", [])
            if rp_list:
                rp_list[-1] = None
            fail_info["rerun_pid"] = rp_list

        _update_task_info(task_dir, fail_info)
        return {"status": "failed", "progress": 0.0, "error": str(e)}
