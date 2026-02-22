import os
import sys
import subprocess
import time
import threading
from typing import Dict, Any, Optional

from pyruns._config import (
    RUN_LOG_DIR, ENV_CONFIG, CONFIG_FILENAME,
    MONITOR_KEY, ERROR_LOG_FILENAME,
)
from pyruns.utils.log_io import append_log
from pyruns.utils.task_io import load_task_info, save_task_info
from pyruns.utils.events import log_emitter
from pyruns.utils import get_logger, get_now_str

logger = get_logger(__name__)



def _prepare_env(
    extra_env: Optional[Dict[str, str]] = None,
    task_dir: Optional[str] = None,
) -> Dict[str, str]:
    """Build a subprocess environment dict.

    Sets UTF-8 encoding, unbuffered output, and optionally points
    ``ENV_CONFIG`` at the task's config.yaml so ``pyruns.read()`` works.
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"    # 避免 Windows GBK 编码问题
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"        # print() 实时刷新
    if task_dir:
        env[ENV_CONFIG] = os.path.join(task_dir, CONFIG_FILENAME)
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items() if k})
    return env


def _get_log_path(task_dir: str, run_index: int) -> str:
    """Return log file path ``run_logs/runN.log`` (1-based), creating the directory if needed."""
    log_dir = os.path.join(task_dir, RUN_LOG_DIR)
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"run{run_index}.log")


def _build_command(meta_cmd, script_path, meta_workdir, config):
    """Build the subprocess command list from task metadata + config.

    Returns (command, workdir) where command may be a list, a string,
    or None (simulation mode).

    When the script uses ``pyruns.load()`` / ``pyruns.read()``, config is
    passed via the ``PYRUNS_CONFIG`` environment variable (set by
    ``_prepare_env``), so NO CLI arguments are appended — only
    ``[python, script]``.  CLI arguments are only built for argparse
    scripts.
    """

    command = meta_cmd or config.get("command")
    workdir = meta_workdir

    if not command and script_path:
        cmd_list: List[str] = [sys.executable, script_path]

        # Detect how the script reads its config
        config_source = "unknown"
        try:
            from pyruns.utils.parse_utils import detect_config_source_fast
            config_source, _ = detect_config_source_fast(script_path)
        except Exception:
            pass

        if config_source == "argparse":
            # ── argparse mode: append CLI args ──
            positional_order: List[str] = []
            try:
                from pyruns.utils.parse_utils import extract_argparse_params
                ap_params = extract_argparse_params(script_path)
                for dest, info in ap_params.items():
                    raw_name = info.get("name", "")
                    if isinstance(raw_name, (list, tuple)):
                        raw_name = raw_name[0] if raw_name else ""
                    if not str(raw_name).startswith("-"):
                        positional_order.append(dest)
            except Exception:
                pass  # fallback: treat everything as optional

            # Positional args first (bare values, in definition order)
            for k in positional_order:
                if k in config and config[k] is not None:
                    v = config[k]
                    if isinstance(v, list):
                        for item in v:
                            cmd_list.append(str(item))
                    else:
                        cmd_list.append(str(v))

            # Optional / remaining args with --prefix
            for k, v in config.items():
                if k in positional_order:
                    continue
                if isinstance(v, bool):
                    if v:
                        cmd_list.append(f"--{k}")
                elif isinstance(v, list):
                    for item in v:
                        cmd_list.append(f"--{k}")
                        cmd_list.append(str(item))
                elif v is not None:
                    cmd_list.append(f"--{k}")
                    cmd_list.append(str(v))
        else:
            # pyruns_load / pyruns_read / unknown:
            # Config is passed via PYRUNS_CONFIG env var, no CLI args needed
            logger.debug("Script uses %s mode — skipping CLI args", config_source)

        command = cmd_list
        if not workdir:
            workdir = os.path.dirname(script_path)

    return command, workdir


def run_task_worker(
    task_dir: str,
    name: str,
    created_at: str,
    config: Dict[str, Any],
    env_vars: Optional[Dict[str, str]] = None,
    run_index: int = 1,
) -> Dict[str, Any]:
    """
    Worker function executed in a separate thread/process.

    run_index: 1-based run number (1 = first run, 2 = second run, …)
    """
    logger.info("Task %s starting  run=#%d", name, run_index)
    now_str = get_now_str()

    log_path = _get_log_path(task_dir, run_index)

    # ── 从 task_info.json 读取内部元数据 ──
    task_meta = load_task_info(task_dir)
    script_path = task_meta.get("script")
    meta_cmd = task_meta.get("cmd")
    meta_workdir = task_meta.get("workdir")

    # 1. Update Status to Running + append start_times
    start_str = now_str
    
    start_log = f"[PYRUNS] ⌘⌘⌘⌘⌘ Task {name} started at {start_str} ⌘⌘⌘⌘⌘\n"
    append_log(log_path, start_log)
    log_emitter.emit(name, start_log.replace('\n', '\r\n'))

    # Remove persisted _run_index (no longer needed once running)
    task_meta.pop("_run_index", None)
    task_meta.update({
        "status": "running",
        "progress": 0.0,
    })

    # 2. Build Command
    command, workdir = _build_command(
        meta_cmd, script_path, meta_workdir, config
    )

    logger.debug("Built command: %s  workdir=%s", command, workdir)

    # 3. Execute
    status = "failed"
    progress = 0.0
    try:
        if command:
            env = _prepare_env(env_vars, task_dir=task_dir)
            
            # Avoid [WinError 267] Invalid directory name on Windows
            # if the user deleted/moved the original script directory between reruns
            if workdir and not os.path.isdir(workdir):
                logger.warning("Workdir '%s' is invalid, falling back to task_dir '%s'", workdir, task_dir)
                workdir = task_dir

            proc = subprocess.Popen(
                command,
                shell=isinstance(command, str),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=workdir,
                env=env,
            )
            # Store PID — append to pids array immediately
            pids = task_meta.get("pids", [])
            pids.append(proc.pid)
            task_meta["pids"] = pids

            # Single write to disk for "running" state + PID
            save_task_info(task_dir, task_meta)

            # Tee pattern: reader thread reads PIPE → file + emit
            def _tee_output():
                with open(log_path, "ab") as lf:
                    # use read1 so it doesn't block waiting to fill 4096 bytes
                    for chunk in iter(lambda: proc.stdout.read1(4096), b''):
                        if not chunk:
                            break
                        # A. Persist to disk immediately
                        lf.write(chunk)
                        lf.flush()
                        # B. Broadcast to subscribed UI sessions
                        text = chunk.decode("utf-8", errors="replace")
                        text = text.replace('\n', '\r\n')
                        log_emitter.emit(name, text)

            reader_thread = threading.Thread(
                target=_tee_output, daemon=True,
            )
            reader_thread.start()
            ret = proc.wait()
            reader_thread.join(timeout=5)

            status = "completed" if ret == 0 else "failed"
            progress = 1.0 if ret == 0 else 0.0
        else:
            raise NotImplementedError("No command to run (simulation mode not implemented)")

        end_str = get_now_str()


        # 4. Final update
        meta_final = load_task_info(task_dir)
        
        finish_log = f"[PYRUNS] ⌘⌘⌘⌘⌘ Task {name} finished at {end_str} ⌘⌘⌘⌘⌘\n"
        append_log(log_path, finish_log)
        log_emitter.emit(name, finish_log.replace('\n', '\r\n'))
        
        # ERROR LOG LOGIC: If failed, move run log content to error.log
        if status == "failed":
            try:
                # Read runN.log content
                content = ""
                if os.path.exists(log_path):
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                
                # Append to error.log with separator
                err_log_path = os.path.join(task_dir, RUN_LOG_DIR, ERROR_LOG_FILENAME)
                separator = f"\n\n{'='*40}\n[PYRUNS] Run #{run_index} FAILED at {end_str}\nReason: Exit Code {proc.returncode if 'proc' in locals() else 'Unknown'}\n{'='*40}\n"
                
                # Write to error.log (OVERWRITE mode)
                with open(err_log_path, "w", encoding="utf-8") as f:
                    f.write(content + separator)
                
                # Try to delete original log (retry a few times for Windows locks)
                for i in range(10):
                    try:
                        if os.path.exists(log_path):
                            os.remove(log_path)
                        break
                    except OSError:
                        time.sleep(0.2)
                else:
                    logger.warning("Could not remove original log after migration: %s", log_path)
            except Exception as e:
                logger.error("Failed to migrate error log: %s", e)

        # ONLY update start/finish times if successful to increment run_index natively
        if status == "completed":
            start_times = meta_final.get("start_times", [])
            start_times.append(start_str)
            finish_times = meta_final.get("finish_times", [])
            finish_times.append(end_str)
            
            monitors = meta_final.get(MONITOR_KEY, [])
            while len(monitors) < run_index:
                monitors.append({})

            meta_final.update({
                "start_times": start_times,
                "finish_times": finish_times,
                "monitors": monitors,
            })

        meta_final.update({
            "status": status,
            "progress": progress,
        })
        save_task_info(task_dir, meta_final)
        logger.info("Task %s finished  status=%s", name, status)
        return {"status": status, "progress": progress}

    except Exception as e:
        meta_err = load_task_info(task_dir)

        meta_err.update({
            "status": "failed",
            "progress": 0.0,
        })
        save_task_info(task_dir, meta_err)
        logger.error("Task %s failed with exception: %s", name, e)
        
        # Write exception to error.log
        try:
            err_log_path = os.path.join(task_dir, RUN_LOG_DIR, ERROR_LOG_FILENAME)
            timestamp = get_now_str()
            separator = f"\n\n{'!'*40}\n[PYRUNS] INTERNAL ERROR at {timestamp}\n{task_dir=} {workdir=}\n{'!'*40}\n"
            with open(err_log_path, "w", encoding="utf-8") as f:
                f.write(f"{separator}{str(e)}\n\n")
        except Exception:
            pass
            
        return {"status": "failed", "progress": 0.0, "error": str(e)}
