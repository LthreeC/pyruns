"""Run a single task as a subprocess and persist its lifecycle."""

from __future__ import annotations

import codecs
import os
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

from pyruns._config import (
    CONFIG_FILENAME,
    ENV_KEY_CONFIG,
    ERROR_LOG_FILENAME,
    RECORDS_KEY,
    RUN_LOGS_DIR,
    TRACKS_KEY,
)
from pyruns.utils import get_logger, get_now_str
from pyruns.utils.events import log_emitter
from pyruns.utils.info_io import (
    ensure_run_slot,
    load_task_info,
    save_task_info,
    update_task_info,
)
from pyruns.utils.log_io import append_log

logger = get_logger(__name__)


def _prepare_env(
    extra_env: Optional[Dict[str, str]] = None,
    task_dir: Optional[str] = None,
) -> Dict[str, str]:
    """Build the subprocess environment."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if task_dir:
        env[ENV_KEY_CONFIG] = os.path.join(task_dir, CONFIG_FILENAME)
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items() if k})
    return env


def _get_log_path(task_dir: str, run_index: int) -> str:
    """Return ``run_logs/runN.log`` and create the directory when needed."""
    log_dir = os.path.join(task_dir, RUN_LOGS_DIR)
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"run{run_index}.log")


def _build_command(
    meta_cmd,
    script_path,
    meta_workdir,
    config,
    run_mode: str = "config",
) -> Tuple[Any, Optional[str]]:
    """Build the subprocess command list from task metadata + config."""
    command = meta_cmd or config.get("command")
    workdir = meta_workdir

    if not command and script_path:
        cmd_list: List[str] = [sys.executable, script_path]

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
                pass

            for key in positional_order:
                if key in config and config[key] is not None:
                    value = config[key]
                    if isinstance(value, list):
                        for item in value:
                            cmd_list.append(str(item))
                    else:
                        cmd_list.append(str(value))

            for key, value in config.items():
                if key in positional_order:
                    continue
                if isinstance(value, bool):
                    if value:
                        cmd_list.append(f"--{key}")
                elif isinstance(value, list):
                    for item in value:
                        cmd_list.append(f"--{key}")
                        cmd_list.append(str(item))
                elif value is not None:
                    cmd_list.append(f"--{key}")
                    cmd_list.append(str(value))
        elif config_source == "hydra":
            raise RuntimeError(
                "Hydra script detected. Use Args mode in Generator (run_mode='args')."
            )

        command = cmd_list
        if not workdir:
            workdir = os.path.dirname(script_path)

    return command, workdir


def _append_error_summary(
    task_dir: str,
    *,
    run_index: int,
    title: str,
    detail_lines: List[str],
) -> None:
    """Append one failure/error summary block into ``error.log``."""
    err_log_path = os.path.join(task_dir, RUN_LOGS_DIR, ERROR_LOG_FILENAME)
    os.makedirs(os.path.dirname(err_log_path), exist_ok=True)
    block = (
        f"\n\n{'=' * 70}\n"
        f"[PYRUNS] {title}\n"
        + "\n".join(detail_lines)
        + f"\n{'=' * 70}\n"
    )
    with open(err_log_path, "a", encoding="utf-8") as f:
        f.write(block)


def run_task_worker(
    task_dir: str,
    name: str,
    created_at: str,
    config: Dict[str, Any],
    env_vars: Optional[Dict[str, str]] = None,
    run_index: int = 1,
) -> Dict[str, Any]:
    """Worker function executed in a separate thread/process."""
    logger.info("Task %s starting  run=#%d", name, run_index)

    log_path = _get_log_path(task_dir, run_index)
    task_meta = load_task_info(task_dir)
    script_path = task_meta.get("script")
    meta_cmd = task_meta.get("cmd")
    meta_workdir = task_meta.get("workdir")

    workspace_dir = os.path.dirname(os.path.dirname(task_dir))
    script_info_path = os.path.join(workspace_dir, "script_info.json")
    if os.path.exists(script_info_path):
        try:
            with open(script_info_path, "r", encoding="utf-8") as f:
                s_info = __import__("json").load(f)
            info_script = s_info.get("script_path")
            if info_script and os.path.exists(info_script):
                script_path = info_script
        except Exception as exc:
            logger.warning("Failed to read script_info.json from %s: %s", workspace_dir, exc)

    command = None
    workdir = None
    proc = None
    status = "failed"
    progress = 0.0
    start_str = ""
    end_str = ""

    try:
        command, workdir = _build_command(
            meta_cmd,
            script_path,
            meta_workdir,
            config,
            run_mode=str(task_meta.get("run_mode", "config") or "config"),
        )
        logger.debug("Built command: %s  workdir=%s", command, workdir)

        if not command:
            raise NotImplementedError("No command to run (simulation mode not implemented)")

        env = _prepare_env(env_vars, task_dir=task_dir)
        env["PYRUNS_RUN_INDEX"] = str(run_index)

        if workdir and not os.path.isdir(workdir):
            fallback = os.path.dirname(script_path) if script_path else os.getcwd()
            logger.warning(
                "Workdir '%s' is invalid, falling back to script dir '%s'",
                workdir,
                fallback,
            )
            workdir = fallback

        proc = subprocess.Popen(
            command,
            shell=isinstance(command, str),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=workdir,
            env=env,
        )

        start_str = get_now_str()
        start_log = f"[PYRUNS] Task {name} started at {start_str}\n"
        append_log(log_path, start_log)
        log_emitter.emit(name, start_log.replace("\n", "\r\n"))

        def _mark_started(info: Dict[str, Any]) -> None:
            slot = ensure_run_slot(info, run_index)
            info["status"] = "running"
            info["progress"] = 0.0
            info["start_times"][slot] = start_str
            info["pids"][slot] = proc.pid

        update_task_info(task_dir, _mark_started)

        def _tee_output() -> None:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            with open(log_path, "ab") as lf:
                for chunk in iter(lambda: proc.stdout.read1(4096), b""):
                    if not chunk:
                        break
                    lf.write(chunk)
                    lf.flush()
                    text = decoder.decode(chunk)
                    text = text.replace("\r\n", "\n").replace("\n", "\r\n")
                    log_emitter.emit(name, text)
                tail = decoder.decode(b"", final=True)
                if tail:
                    tail = tail.replace("\r\n", "\n").replace("\n", "\r\n")
                    log_emitter.emit(name, tail)

        reader_thread = threading.Thread(target=_tee_output, daemon=True)
        reader_thread.start()
        ret = proc.wait()
        reader_thread.join(timeout=5)

        status = "completed" if ret == 0 else "failed"
        progress = 1.0 if ret == 0 else 0.0
        end_str = get_now_str()

        finish_log = f"[PYRUNS] Task {name} finished at {end_str}\n"
        append_log(log_path, finish_log)
        log_emitter.emit(name, finish_log.replace("\n", "\r\n"))

        def _mark_finished(info: Dict[str, Any]) -> None:
            slot = ensure_run_slot(info, run_index)
            info["status"] = status
            info["progress"] = progress
            if start_str and not info["start_times"][slot]:
                info["start_times"][slot] = start_str
            info["finish_times"][slot] = end_str
            if proc is not None:
                info["pids"][slot] = proc.pid
            if RECORDS_KEY not in info:
                info[RECORDS_KEY] = []
            if TRACKS_KEY not in info:
                info[TRACKS_KEY] = []

        update_task_info(task_dir, _mark_finished)

        if status == "failed":
            _append_error_summary(
                task_dir,
                run_index=run_index,
                title=f"Run #{run_index} failed at {end_str}",
                detail_lines=[
                    f"reason=exit_code {ret}",
                    f"command={command!r}",
                    f"workdir={workdir!r}",
                    f"log={log_path}",
                ],
            )

        logger.info("Task %s finished  status=%s", name, status)
        return {"status": status, "progress": progress}

    except Exception as exc:
        end_str = get_now_str()

        def _mark_error(info: Dict[str, Any]) -> None:
            slot = ensure_run_slot(info, run_index)
            info["status"] = "failed"
            info["progress"] = 0.0
            if start_str and not info["start_times"][slot]:
                info["start_times"][slot] = start_str
            if end_str:
                info["finish_times"][slot] = end_str
            if proc is not None:
                info["pids"][slot] = proc.pid

        update_task_info(task_dir, _mark_error)

        detail_lines = [
            f"exception={type(exc).__name__}: {exc}",
            f"command={command!r}",
            f"workdir={workdir!r}",
            f"task_dir={task_dir!r}",
        ]
        _append_error_summary(
            task_dir,
            run_index=run_index,
            title=f"Internal error during run #{run_index} at {end_str}",
            detail_lines=detail_lines,
        )

        block = (
            f"\n{'=' * 70}\n"
            f"[PYRUNS ERROR] Task {name} failed\n"
            + "\n".join(detail_lines)
            + f"\n{'=' * 70}"
        )
        logger.error("%s", block)
        return {"status": "failed", "progress": 0.0, "error": str(exc)}
