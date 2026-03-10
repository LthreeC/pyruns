"""
Executor — runs a single task as a subprocess.

Three phases:
  1. ``_prepare_env()``   — build the environment dict (PYTHONIOENCODING, config path, …)
  2. ``_build_command()``  — detect script type (argparse / pyruns) and assemble argv
  3. ``run_task_worker()`` — spawn the subprocess, tee stdout to log + EventBus, update status
"""
import os
import sys
import subprocess
import time
import threading
import codecs
from typing import Dict, Any, Optional, List

from .config_manager import ConfigManager
from .._config import (
    ENV_KEY_CONFIG, CONFIG_FILENAME, CONFIG_DEFAULT_FILENAME,
    RECORDS_KEY, RUN_LOGS_DIR, ERROR_LOG_FILENAME,
    TASK_INFO_FILENAME,
)
from pyruns.utils.log_io import append_log
from pyruns.utils.info_io import load_task_info, save_task_info, normalize_run_history
from pyruns.utils.events import log_emitter
from pyruns.utils import get_logger, get_now_str

logger = get_logger(__name__)


def _prepare_env(
    extra_env: Optional[Dict[str, str]] = None,
    task_dir: Optional[str] = None,
) -> Dict[str, str]:
    """Build a subprocess environment dict.

    Sets UTF-8 encoding, unbuffered output, and optionally points
    ``ENV_KEY_CONFIG`` at the task's config.yaml so ``pyruns.load()`` works.
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"    # 避免 Windows GBK 编码问题
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"        # print() 实时刷新
    if task_dir:
        env[ENV_KEY_CONFIG] = os.path.join(task_dir, CONFIG_FILENAME)
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items() if k})
    return env


def _get_log_path(task_dir: str, run_index: int) -> str:
    """Return log file path ``run_logs/runN.log`` (1-based), creating the directory if needed."""
    log_dir = os.path.join(task_dir, RUN_LOGS_DIR)
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"run{run_index}.log")


# _normalize_run_history is now in utils/info_io.py  →  normalize_run_history


def _build_command(meta_cmd, script_path, meta_workdir, config, run_mode: str = "config"):
    """Build the subprocess command list from task metadata + config.

    Returns (command, workdir) where command may be a list, a string,
    or None (simulation mode).

    When the script uses ``pyruns.load()``, config is
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

        mode = str(run_mode or "config").strip().lower()

        if mode == "args":
            try:
                from pyruns.utils.parse_utils import split_cli_args
                run_script = str(config.get("run_script", "") or "").strip()
                script_cmd = split_cli_args(run_script) if run_script else []
                cmd_list = script_cmd if script_cmd else [sys.executable, script_path]
                cli_args = split_cli_args(config.get("args", ""))
            except Exception:
                cli_args = []
            cmd_list.extend(cli_args)
        elif config_source == "argparse":
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
        elif config_source == "hydra":
            raise RuntimeError(
                "Hydra script detected. Use Args mode in Generator (run_mode='args')."
            )
        else:
            # pyruns_load / unknown:
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

    # ── 强制参考当前 Run Root 的 script_info.json ──
    workspace_dir = os.path.dirname(os.path.dirname(task_dir))
    script_info_path = os.path.join(workspace_dir, "script_info.json")
    if os.path.exists(script_info_path):
        try:
            import json
            with open(script_info_path, "r", encoding="utf-8") as f:
                s_info = json.load(f)
                info_script = s_info.get("script_path")
                if info_script and os.path.exists(info_script):
                    script_path = info_script
        except Exception as e:
            logger.warning("Failed to read script_info.json from %s: %s", workspace_dir, e)

    # 1. Update Status to Running + append start_times
    start_str = now_str
    
    start_log = f"[PYRUNS] ⌘⌘⌘⌘⌘ Task {name} started at {start_str} ⌘⌘⌘⌘⌘\n"
    append_log(log_path, start_log)
    log_emitter.emit(name, start_log.replace('\n', '\r\n'))

    # Remove stale queued marker, keep a single canonical run_index field.
    task_meta.pop("_run_index", None)
    task_meta.update({
        "status": "running",
        "progress": 0.0,
        "run_index": run_index,  # explicitly store run_index
    })

    # 2. Build Command
    command, workdir = _build_command(
        meta_cmd,
        script_path,
        meta_workdir,
        config,
        run_mode=str(task_meta.get("run_mode", "config") or "config"),
    )

    logger.debug("Built command: %s  workdir=%s", command, workdir)

    # 3. Execute
    status = "failed"
    progress = 0.0
    try:
        if command:
            env = _prepare_env(env_vars, task_dir=task_dir)
            env["PYRUNS_RUN_INDEX"] = str(run_index)
            
            # Avoid [WinError 267] Invalid directory name on Windows
            # if the user deleted/moved the original script directory between reruns
            if workdir and not os.path.isdir(workdir):
                fallback = os.path.dirname(script_path) if script_path else os.getcwd()
                logger.warning("Workdir '%s' is invalid, falling back to script dir '%s'", workdir, fallback)
                workdir = fallback

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
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                with open(log_path, "ab") as lf:
                    # use read1 so it doesn't block waiting to fill 4096 bytes
                    for chunk in iter(lambda: proc.stdout.read1(4096), b''):
                        if not chunk:
                            break
                        # A. Persist to disk immediately
                        lf.write(chunk)
                        lf.flush()
                        # B. Broadcast to subscribed UI sessions
                        text = decoder.decode(chunk)
                        text = text.replace('\r\n', '\n').replace('\n', '\r\n')
                        log_emitter.emit(name, text)
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        tail = tail.replace('\r\n', '\n').replace('\n', '\r\n')
                        log_emitter.emit(name, tail)

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
        meta_final = load_task_info(task_dir) or dict(task_meta)
        
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
                err_log_path = os.path.join(task_dir, RUN_LOGS_DIR, ERROR_LOG_FILENAME)
                separator = (
                    f"\n\n{'='*40}\n"
                    f"[PYRUNS] Run #{run_index} FAILED at {end_str}\n"
                    f"Reason: Exit Code {proc.returncode if 'proc' in locals() else 'Unknown'}\n"
                    f"Command: {command!r}\n"
                    f"Workdir: {workdir!r}\n"
                    f"{'='*40}\n"
                )
                
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
            
            # Make sure we have a "records" array large enough for `run_index`
            records = meta_final.get(RECORDS_KEY, [])
            while len(records) < run_index:
                records.append({})

            meta_final.update({
                "start_times": start_times,
                "finish_times": finish_times,
                RECORDS_KEY: records,
            })

        normalize_run_history(meta_final)

        meta_final.update({
            "status": status,
            "progress": progress,
        })
        save_task_info(task_dir, meta_final)
        logger.info("Task %s finished  status=%s", name, status)
        return {"status": status, "progress": progress}

    except Exception as e:
        meta_err = load_task_info(task_dir) or dict(task_meta)
        normalize_run_history(meta_err)

        meta_err.update({
            "status": "failed",
            "progress": 0.0,
        })
        save_task_info(task_dir, meta_err)
        run_mode = str(task_meta.get("run_mode", "config") or "config")
        detail_text = "\n".join([
            f"exception={type(e).__name__}: {e}",
            f"command={command!r}",
            f"workdir={workdir!r}",
            f"run_mode={run_mode}",
        ])
        block = (
            f"\n{'=' * 70}\n"
            f"[PYRUNS ERROR] Task {name} failed\n"
            f"{detail_text}\n"
            f"{'=' * 70}"
        )
        logger.error("%s", block)
        
        # Write exception to error.log
        try:
            err_log_path = os.path.join(task_dir, RUN_LOGS_DIR, ERROR_LOG_FILENAME)
            timestamp = get_now_str()
            separator = (
                f"\n\n{'!'*70}\n"
                f"[PYRUNS] INTERNAL ERROR at {timestamp}\n"
                f"{task_dir=} {workdir=}\n"
                f"{'!'*70}\n"
            )
            with open(err_log_path, "w", encoding="utf-8") as f:
                f.write(f"{separator}{detail_text}\n{'!'*70}\n\n")
        except Exception:
            pass
            
        return {"status": "failed", "progress": 0.0, "error": str(e)}

# cmd /c dir
# powershell -Command ls