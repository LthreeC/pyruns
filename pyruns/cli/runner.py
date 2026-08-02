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
    startup_timeout: float = 5.0,
) -> bool:
    """Start a detached runner and wait only until it has claimed the tasks."""

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
    deadline = time.monotonic() + max(0.1, float(startup_timeout))
    while time.monotonic() < deadline:
        exit_code = process.poll()
        ready = True
        owned_active = 0
        all_final = True
        for name in names:
            info = load_task_info(task_dirs[name]) or {}
            status = str(info.get("status", "") or "").lower()
            started_new_run = _run_index(info) > before[name]
            owned = status in _ACTIVE_STATUSES and _runner_token(info) == submission_token
            finished = status in _FINAL_STATUSES and started_new_run
            if owned:
                owned_active += 1
            if not finished:
                all_final = False
            if not owned and not finished:
                ready = False
                break
        if ready and (owned_active > 0 or (all_final and exit_code in {0, 1})):
            return True
        if exit_code is not None:
            return False
        time.sleep(0.05)
    if process.poll() is None:
        try:
            kill_process(process.pid)
        except Exception:
            pass
    return False
