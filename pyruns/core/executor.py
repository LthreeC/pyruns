"""Run a single task as a subprocess and persist its lifecycle."""

from __future__ import annotations

import codecs
import os
import shlex
import subprocess
import sys
import tempfile
import threading
from typing import Any, Dict, List, Optional, Tuple

from pyruns._config import (
    CONFIG_FILENAME,
    ENV_KEY_CONFIG,
    ERROR_LOG_FILENAME,
    RECORDS_KEY,
    RUN_LOGS_DIR,
    SHELL_CONFIG_FILENAME,
    TASK_KIND_CONFIG,
    TASK_KIND_SHELL,
    TRACKS_KEY,
)
from pyruns.utils import get_logger, get_now_str
from pyruns.utils.events import log_emitter
from pyruns.utils.info_io import (
    ensure_run_slot,
    load_task_info,
    update_task_info,
)
from pyruns.utils.log_io import normalize_log_newlines
from pyruns.utils.shell_runtime import get_shell_runtime_for_task
from pyruns.utils.task_files import normalize_task_kind, resolve_task_config_file

logger = get_logger(__name__)


def _lifecycle_banner(phase: str, name: str, timestamp: str) -> str:
    """Build a more visible lifecycle banner for task start/finish logs."""

    title = phase.upper()
    return (
        f"[PYRUNS] {'=' * 20} {title} {'=' * 20}\n"
        f"[PYRUNS] Task {name} {phase.lower()}ed at {timestamp}\n"
        f"[PYRUNS] {'=' * (42 + len(title))}\n"
    )


def _prepend_pythonpath(env: Dict[str, str], path: str) -> None:
    """Ensure child scripts can import the same pyruns package as the parent."""

    if not path or not os.path.isdir(path):
        return

    existing = str(env.get("PYTHONPATH", "") or "")
    entries = [entry for entry in existing.split(os.pathsep) if entry]
    normalized_entries = {
        os.path.normcase(os.path.abspath(entry))
        for entry in entries
    }
    normalized_path = os.path.normcase(os.path.abspath(path))
    if normalized_path in normalized_entries:
        return

    env["PYTHONPATH"] = path if not existing else f"{path}{os.pathsep}{existing}"


def _prepare_env(
    extra_env: Optional[Dict[str, str]] = None,
    *,
    task_dir: Optional[str] = None,
    task_kind: str = TASK_KIND_CONFIG,
    config_file: str = CONFIG_FILENAME,
) -> Dict[str, str]:
    """Build the subprocess environment."""

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if task_dir and normalize_task_kind(task_kind) == TASK_KIND_CONFIG:
        env[ENV_KEY_CONFIG] = os.path.join(task_dir, config_file or CONFIG_FILENAME)
    else:
        env.pop(ENV_KEY_CONFIG, None)
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items() if k})
    package_parent = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
    _prepend_pythonpath(env, package_parent)
    return env


def _get_log_path(task_dir: str, run_index: int) -> str:
    """Return ``run_logs/runN.log`` and create the directory when needed."""

    log_dir = os.path.join(task_dir, RUN_LOGS_DIR)
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"run{run_index}.log")


def _resolve_shell_executable(task_dir: str | None = None) -> str:
    """Resolve the shell executable used for shell tasks."""
    runtime = get_shell_runtime_for_task(task_dir)
    shell_path = str(runtime.get("executable", "") or "").strip()
    if shell_path and bool(runtime.get("available", False)):
        return shell_path

    if runtime.get("mode") == "custom":
        raise RuntimeError(
            "shell_mode=custom requires a valid shell_executable in "
            "_pyruns_settings.yaml before running shell tasks."
        )

    raise RuntimeError(
        "Unable to resolve the current terminal shell for shell tasks. "
        "Start pyr from the shell you want to follow, or switch to "
        "shell_mode=custom in _pyruns_settings.yaml."
    )


def _is_posix_shell_executable(shell_path: str) -> bool:
    """Return True when the resolved shell should execute the script directly."""

    name = os.path.basename(shell_path).lower()
    return name in {"bash", "bash.exe", "sh", "sh.exe", "zsh", "zsh.exe", "fish", "fish.exe"}


def _is_powershell_executable(shell_path: str) -> bool:
    """Return True when the resolved shell uses PowerShell script semantics."""

    name = os.path.basename(shell_path).lower()
    return "powershell" in name or name.startswith("pwsh")


def _read_shell_script_body(script_path: str) -> str:
    """Read the stored shell script and strip any shebang for native wrappers."""

    with open(script_path, "r", encoding="utf-8") as handle:
        body = handle.read()

    if body.startswith("#!"):
        _, _, remainder = body.partition("\n")
        body = remainder
    return body.lstrip("\r\n")


def _powershell_utf8_preamble() -> str:
    """Force PowerShell wrapper I/O through UTF-8 for stable log capture."""

    return "\n".join(
        [
            "$__pyrunsUtf8 = [System.Text.UTF8Encoding]::new($false)",
            "[Console]::InputEncoding = $__pyrunsUtf8",
            "[Console]::OutputEncoding = $__pyrunsUtf8",
            "$OutputEncoding = $__pyrunsUtf8",
        ]
    )


def _write_temp_shell_wrapper(
    *,
    suffix: str,
    content: str,
    encoding: str,
    newline: str,
) -> str:
    """Write one temporary wrapper file outside the task directory."""

    fd, wrapper_path = tempfile.mkstemp(prefix="pyruns-shell-", suffix=suffix)
    os.close(fd)
    with open(wrapper_path, "w", encoding=encoding, newline=newline) as handle:
        handle.write(content)
    return wrapper_path


def _materialize_windows_shell_wrapper(
    task_dir: str,
    script_path: str,
    shell_path: str,
) -> Tuple[List[str], str, List[str]]:
    """Create a native Windows wrapper script around the stored shell task body."""

    if _is_posix_shell_executable(shell_path):
        return [shell_path, script_path], task_dir, []

    script_body = _read_shell_script_body(script_path)
    if _is_powershell_executable(shell_path):
        wrapper_body = "\n".join(
            [
                _powershell_utf8_preamble(),
                script_body.rstrip() or "exit 0",
            ]
        )
        wrapper_path = _write_temp_shell_wrapper(
            suffix=".ps1",
            content=wrapper_body,
            encoding="utf-8-sig",
            newline="\r\n",
        )
        return [
            shell_path,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            wrapper_path,
        ], task_dir, [wrapper_path]

    wrapper_lines = [
        "@echo off",
        "setlocal",
        "chcp 65001 >nul",
        script_body.rstrip() or "exit /b 0",
    ]
    wrapper_path = _write_temp_shell_wrapper(
        suffix=".cmd",
        content="\r\n".join(wrapper_lines) + "\r\n",
        encoding="utf-8-sig",
        newline="\r\n",
    )
    return [shell_path, "/d", "/c", wrapper_path], task_dir, [wrapper_path]


def _build_shell_command(task_dir: str, config_file: str) -> Tuple[List[str], str, List[str]]:
    script_path = os.path.join(task_dir, config_file or SHELL_CONFIG_FILENAME)
    if not os.path.exists(script_path):
        raise FileNotFoundError(script_path)
    shell_path = _resolve_shell_executable(task_dir)
    if os.name == "nt":
        return _materialize_windows_shell_wrapper(task_dir, script_path, shell_path)
    return [shell_path, script_path], task_dir, []


def _build_command(
    meta_cmd,
    script_path,
    meta_workdir,
    config,
    *,
    task_kind: str = TASK_KIND_CONFIG,
    task_dir: str | None = None,
    config_file: str = CONFIG_FILENAME,
) -> Tuple[Any, Optional[str], List[str]]:
    """Build the subprocess command list from task metadata + payload."""

    normalized_kind = normalize_task_kind(task_kind)
    if normalized_kind == TASK_KIND_SHELL:
        if not task_dir:
            raise RuntimeError("Shell task execution requires a task directory")
        return _build_shell_command(task_dir, config_file or SHELL_CONFIG_FILENAME)

    command = meta_cmd or config.get("command")
    workdir = meta_workdir

    if not command and script_path:
        cmd_list: List[str] = [sys.executable, script_path]

        from pyruns.utils.parse_utils import detect_config_source_fast, extract_argparse_params

        config_source, _ = detect_config_source_fast(script_path)

        if config_source == "argparse":
            positional_order: List[str] = []
            ap_params: Dict[str, Dict[str, Any]] = {}
            try:
                ap_params = extract_argparse_params(script_path)
                for dest, info in ap_params.items():
                    raw_name = info.get("name", "")
                    if isinstance(raw_name, (list, tuple)):
                        raw_name = raw_name[0] if raw_name else ""
                    if not str(raw_name).startswith("-"):
                        positional_order.append(dest)
            except Exception:
                pass

            def _option_flag_for_key(key: str) -> str:
                info = ap_params.get(key, {}) if isinstance(ap_params, dict) else {}
                flags = info.get("flags")
                if not isinstance(flags, (list, tuple)):
                    raw_name = info.get("name")
                    flags = [raw_name] if raw_name else []
                normalized_flags = [str(flag) for flag in flags if str(flag).startswith("-")]
                long_flags = [flag for flag in normalized_flags if flag.startswith("--")]
                if long_flags:
                    return long_flags[0]
                if normalized_flags:
                    return normalized_flags[0]
                return f"--{key}"

            def _append_option(key: str, value: Any) -> None:
                info = ap_params.get(key, {}) if isinstance(ap_params, dict) else {}
                action = str(info.get("action", "") or "")
                nargs = info.get("nargs")
                flag = _option_flag_for_key(key)

                def _negative_bool_flag(raw_flag: str) -> str:
                    if raw_flag.startswith("--no-"):
                        return raw_flag
                    if raw_flag.startswith("--"):
                        return f"--no-{raw_flag[2:]}"
                    return raw_flag

                if isinstance(value, bool):
                    if action.endswith("BooleanOptionalAction"):
                        cmd_list.append(flag if value else _negative_bool_flag(flag))
                    elif action == "store_false":
                        if not value:
                            cmd_list.append(flag)
                    elif action == "store_true":
                        if value:
                            cmd_list.append(flag)
                    elif key in ap_params:
                        cmd_list.append(flag)
                        cmd_list.append(str(value))
                    elif value:
                        cmd_list.append(flag)
                    return

                if isinstance(value, list):
                    if action != "append" and nargs not in (None, ""):
                        cmd_list.append(flag)
                        cmd_list.extend(str(item) for item in value)
                    else:
                        for item in value:
                            cmd_list.append(flag)
                            cmd_list.append(str(item))
                    return

                if value is not None:
                    cmd_list.append(flag)
                    cmd_list.append(str(value))

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
                _append_option(key, value)
        elif config_source == "hydra":
            raise RuntimeError(
                "Hydra script detected. Use a shell workspace/task to launch it explicitly."
            )
        elif config_source == "unknown":
            raise RuntimeError(
                "Unable to detect script config mode safely. Use a shell workspace/task, "
                "or integrate via argparse / pyruns.load()."
            )

        command = cmd_list
        if not workdir:
            workdir = os.path.dirname(script_path)

    return command, workdir, []


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
    with open(err_log_path, "a", encoding="utf-8") as handle:
        handle.write(block)


def _consume_pending_stop_summary(task_dir: str, run_index: int) -> Dict[str, Any] | None:
    """Pop one pending stop summary for the finished run, if present."""

    captured: Dict[str, Any] = {}

    def _apply(info: Dict[str, Any]) -> None:
        raw = info.get("_pending_stop_summary")
        if not isinstance(raw, dict):
            return
        raw_index = int(raw.get("run_index", 0) or 0)
        if raw_index != int(run_index):
            return
        captured.update(raw)
        info.pop("_pending_stop_summary", None)

    update_task_info(task_dir, _apply)
    return captured or None


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
    task_kind = normalize_task_kind(task_meta.get("config_mode", task_meta.get("task_kind")))
    config_file = resolve_task_config_file(task_meta, task_kind, task_dir)
    script_path = task_meta.get("script")
    meta_cmd = task_meta.get("cmd")
    meta_workdir = task_meta.get("workdir")

    workspace_dir = os.path.dirname(os.path.dirname(task_dir))
    script_info_path = os.path.join(workspace_dir, "script_info.json")
    if task_kind == TASK_KIND_CONFIG and os.path.exists(script_info_path):
        try:
            with open(script_info_path, "r", encoding="utf-8") as handle:
                s_info = __import__("json").load(handle)
            info_script = s_info.get("script_path")
            if info_script and os.path.exists(info_script):
                script_path = info_script
            elif info_script and not os.path.exists(info_script):
                script_basename = os.path.basename(info_script)
                pyruns_parent = os.path.dirname(os.path.dirname(workspace_dir))
                candidate = os.path.join(pyruns_parent, script_basename)
                if os.path.exists(candidate):
                    script_path = candidate
                    logger.info(
                        "script_info path stale (%s), resolved to %s",
                        info_script,
                        candidate,
                    )
        except Exception as exc:
            logger.warning("Failed to read script_info.json from %s: %s", workspace_dir, exc)

    command = None
    workdir = None
    cleanup_paths: List[str] = []
    proc = None
    status = "failed"
    progress = 0.0
    start_str = ""
    end_str = ""

    try:
        command, workdir, cleanup_paths = _build_command(
            meta_cmd,
            script_path,
            meta_workdir,
            config,
            task_kind=task_kind,
            task_dir=task_dir,
            config_file=config_file or (
                SHELL_CONFIG_FILENAME if task_kind == TASK_KIND_SHELL else CONFIG_FILENAME
            ),
        )
        logger.debug("Built command: %s  workdir=%s", command, workdir)

        if not command:
            raise NotImplementedError("No command to run (simulation mode not implemented)")

        env = _prepare_env(
            env_vars,
            task_dir=task_dir,
            task_kind=task_kind,
            config_file=config_file or CONFIG_FILENAME,
        )
        env["PYRUNS_RUN_INDEX"] = str(run_index)

        if workdir and not os.path.isdir(workdir):
            fallback = task_dir if task_kind == TASK_KIND_SHELL else (os.path.dirname(script_path) if script_path else os.getcwd())
            logger.warning(
                "Workdir '%s' is invalid, falling back to '%s'",
                workdir,
                fallback,
            )
            workdir = fallback

        if isinstance(command, str):
            command = shlex.split(command, posix=(os.name != "nt"))

        proc = subprocess.Popen(
            command,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=workdir,
            env=env,
        )

        start_str = get_now_str()
        start_log = _lifecycle_banner("start", name, start_str)
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(start_log)
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
            with open(log_path, "ab") as handle:
                for chunk in iter(lambda: proc.stdout.read1(4096), b""):
                    if not chunk:
                        break
                    handle.write(chunk)
                    handle.flush()
                    text = normalize_log_newlines(decoder.decode(chunk))
                    log_emitter.emit(name, text)
                tail = decoder.decode(b"", final=True)
                if tail:
                    log_emitter.emit(name, normalize_log_newlines(tail))

        reader_thread = threading.Thread(target=_tee_output, daemon=True)
        reader_thread.start()
        ret = proc.wait()
        reader_thread.join(timeout=5)

        status = "completed" if ret == 0 else "failed"
        progress = 1.0 if ret == 0 else 0.0
        end_str = get_now_str()

        finish_log = _lifecycle_banner("finish", name, end_str)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(finish_log)
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
            stop_summary = _consume_pending_stop_summary(task_dir, run_index)
            if stop_summary:
                detail_lines = [f"reason={stop_summary.get('reason', 'stopped')}"]
                detail_lines.extend(list(stop_summary.get("detail_lines", []) or []))
                detail_lines.extend(
                    [
                        f"exit_code={ret}",
                        f"command={command!r}",
                        f"workdir={workdir!r}",
                        f"log={log_path}",
                    ]
                )
                _append_error_summary(
                    task_dir,
                    run_index=run_index,
                    title=f"Run #{run_index} {stop_summary.get('event', 'stopped')} at {end_str}",
                    detail_lines=detail_lines,
                )
            else:
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
    finally:
        for cleanup_path in cleanup_paths:
            try:
                if cleanup_path and os.path.exists(cleanup_path):
                    os.remove(cleanup_path)
            except OSError:
                logger.debug("Failed to remove temporary shell wrapper: %s", cleanup_path)

