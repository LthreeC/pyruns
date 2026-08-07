"""Submit CLI task runs to a process that outlives the invoking terminal command."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from typing import Any

from pyruns._config import ENV_KEY_CLI_SHELL_EXECUTABLE
from pyruns.utils.info_io import load_task_info
from pyruns.utils.process_utils import kill_process
from pyruns.utils.shell_runtime import get_follow_shell_runtime


_ACTIVE_STATUSES = {"queued", "running"}
_FINAL_STATUSES = {"completed", "failed", "cancelled"}
DEFAULT_STARTUP_TIMEOUT_SEC = 15.0


def _run_index(info: dict[str, Any]) -> int:
    try:
        return int(info.get("run_index", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _runner_token(info: dict[str, Any]) -> str:
    parts = str(info.get("runner_id", "") or "").rsplit(":", 2)
    return parts[2] if len(parts) == 3 else ""


def _detached_popen(command: list[str], env: dict[str, str]) -> subprocess.Popen:
    package_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
        "cwd": package_root,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def submit_cli_tasks(
    tm,
    task_names: list[str],
    *,
    execution_mode: str = "thread",
    max_workers: int = 1,
    startup_timeout: float | None = None,
) -> bool:
    """Start a detached runner and wait until it reports that all tasks were claimed."""

    names = [str(name) for name in task_names if str(name)]
    if not names:
        return False

    task_dirs = {
        str(task.get("name")): str(task.get("dir"))
        for task in getattr(tm, "tasks", [])
        if task and str(task.get("name", "")) in names
    }
    if len(task_dirs) != len(set(names)):
        return False

    before = {name: _run_index(load_task_info(task_dirs[name]) or {}) for name in names}
    workspace = os.path.dirname(os.path.abspath(str(tm.tasks_dir)))
    submission_token = uuid.uuid4().hex
    startup_file = os.path.join(
        str(tm.tasks_dir),
        f".runner-startup-{submission_token}.json",
    )
    command = [
        sys.executable,
        "-m",
        "pyruns.cli.detached_runner",
        "--workspace",
        workspace,
        "--mode",
        execution_mode,
        "--workers",
        str(max(1, int(max_workers))),
        "--submission-token",
        submission_token,
        "--startup-file",
        startup_file,
        "--tasks-json",
        json.dumps(names),
    ]

    env = os.environ.copy()
    runtime = get_follow_shell_runtime()
    shell_executable = str(runtime.get("executable", "") or "").strip()
    if shell_executable:
        env[ENV_KEY_CLI_SHELL_EXECUTABLE] = shell_executable

    try:
        process = _detached_popen(command, env)
    except OSError:
        return False
    timeout_seconds = DEFAULT_STARTUP_TIMEOUT_SEC if startup_timeout is None else float(startup_timeout)
    deadline = time.monotonic() + max(0.05, timeout_seconds)
    try:
        while True:
            try:
                with open(startup_file, "r", encoding="utf-8") as handle:
                    startup = json.load(handle)
            except (FileNotFoundError, OSError, ValueError, TypeError):
                startup = {}
            startup_status = str(startup.get("status", "") or "").lower()
            if startup_status == "ready":
                return True
            if startup_status == "error":
                return False

            exit_code = process.poll()
            owned_active = 0
            all_final = True
            for name in names:
                info = load_task_info(task_dirs[name]) or {}
                status = str(info.get("status", "") or "").lower()
                started_new_run = _run_index(info) > before[name]
                if status in _ACTIVE_STATUSES and _runner_token(info) == submission_token:
                    owned_active += 1
                if not (status in _FINAL_STATUSES and started_new_run):
                    all_final = False
            if exit_code is not None:
                return bool(all_final and exit_code in {0, 1})

            if time.monotonic() >= deadline:
                if owned_active:
                    # A partially claimed batch must be left with its owning runner;
                    # killing it here can strand task state or terminate real work.
                    return True
                try:
                    kill_process(process.pid)
                except Exception:
                    pass
                return False
            time.sleep(0.05)
    finally:
        try:
            os.remove(startup_file)
        except FileNotFoundError:
            pass
        except OSError:
            pass
