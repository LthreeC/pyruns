"""
CLI display utilities for task tables, jobs, and task details.
"""

from __future__ import annotations

import os
import shutil
from typing import Any, Dict

from pyruns.utils.config_utils import preview_config_line

# Enable ANSI escape sequences on Windows terminals that support VT100.
if os.name == "nt":
    os.system("")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"

_STATUS_STYLES = {
    "running": "\033[1;32m",
    "queued": "\033[1;33m",
    "completed": "\033[36m",
    "failed": "\033[1;31m",
    "pending": "\033[90m",
}

_STATUS_ICONS = {
    "running": ">",
    "queued": "~",
    "completed": "+",
    "failed": "!",
    "pending": ".",
}


def _colored(text: str, style: str) -> str:
    return f"{style}{text}{_RESET}" if style else text


def _status_str(status: str, runs: int = 1) -> str:
    icon = _STATUS_ICONS.get(status, "?")
    style = _STATUS_STYLES.get(status, "")
    label = status.capitalize()
    if status in ("completed", "failed") and runs > 1:
        label = f"{label}({runs})"
    return _colored(f"{icon} {label:<10}", style)


def _truncate(text: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return "." * max_len
    return text[: max_len - 3] + "..."


def _get_terminal_width() -> int:
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


def _separator(width: int) -> str:
    return f"  {'-' * max(8, width - 4)}"


def print_task_table(tasks: list[Dict[str, Any]], title: str = "Tasks") -> None:
    """Print a formatted task list table to stdout."""
    if not tasks:
        print(f"\n  {_DIM}No tasks found.{_RESET}\n")
        return

    terminal_width = _get_terminal_width()
    index_width = max(3, len(str(len(tasks))))
    status_width = 13
    time_width = 19
    name_width = max(12, terminal_width - index_width - status_width - time_width - 10)

    header = (
        f"  {_BOLD}{'#':<{index_width}}  {'Status':<{status_width}} "
        f"{'Name':<{name_width}}  {'Created':<{time_width}}{_RESET}"
    )
    separator = _separator(terminal_width)

    print(f"\n  {_BOLD}{title}{_RESET}  ({len(tasks)} total)")
    print(separator)
    print(header)
    print(separator)

    for index, task in enumerate(tasks, 1):
        name = _truncate(task.get("name") or "unnamed", name_width)
        created = (task.get("created_at") or "")[:time_width]
        status = task.get("status") or "pending"
        runs = len(task.get("start_times") or [])
        status_cell = _status_str(status, runs)
        print(f"  {index:<{index_width}}  {status_cell} {name:<{name_width}}  {_DIM}{created}{_RESET}")

    print(separator)
    print()


def print_jobs(tasks: list[Dict[str, Any]]) -> None:
    """Print running and queued tasks in a Linux-jobs-like format."""
    active = [task for task in tasks if task.get("status") in ("running", "queued")]
    if not active:
        print(f"\n  {_DIM}No active jobs.{_RESET}\n")
        return

    print()
    for index, task in enumerate(active, 1):
        status = task.get("status", "unknown").capitalize()
        name = task.get("name", "unnamed")
        style = _STATUS_STYLES.get(task.get("status", ""), "")
        marker = "+" if index == 1 else " "
        print(f"  [{index}]{marker}  {_colored(f'{status:<10}', style)}  {name}")
    print()


def print_task_detail(task: Dict[str, Any]) -> None:
    """Print detailed information for one task."""
    terminal_width = _get_terminal_width()
    separator = _separator(terminal_width)

    print(f"\n  {_BOLD}{task.get('name', 'unnamed')}{_RESET}")
    print(separator)
    starts = task.get("start_times", [])
    runs = len(starts)
    print(f"  Status:     {_status_str(task.get('status', 'pending'), runs)}")
    print(f"  Created:    {task.get('created_at', 'N/A')}")
    print(f"  Directory:  {_DIM}{task.get('dir', 'N/A')}{_RESET}")

    finishes = task.get("finish_times", [])
    if starts:
        print(f"  Runs:       {len(starts)}")
        print(f"  Last start: {starts[-1]}")
    if finishes:
        print(f"  Last end:   {finishes[-1]}")

    if task.get("pinned"):
        print("  Pinned:     yes")

    env = task.get("env") or {}
    if env:
        env_preview = ", ".join(f"{key}={value}" for key, value in sorted(env.items()))
        print(f"  Env:        {_DIM}{_truncate(env_preview, max(24, terminal_width - 20))}{_RESET}")

    notes = (task.get("notes") or "").strip()
    if notes:
        print(f"  Notes:      {_DIM}{notes}{_RESET}")

    load_error = task.get("_load_error")
    if load_error:
        print(f"  Load error: {_colored(load_error, _STATUS_STYLES['failed'])}")

    config = task.get("config", {})
    if config:
        preview = preview_config_line(config, max_items=8)
        if preview:
            print(f"  Config:     {_DIM}{preview}{_RESET}")

    print(separator)
    print()
