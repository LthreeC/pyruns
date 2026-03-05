"""
CLI display utilities — terminal table rendering for tasks and jobs.

Provides ANSI-colored, aligned table output for task lists and job status,
reusing the same sort/filter logic as the UI via ``pyruns.utils.sort_utils``.
"""
import os
import shutil
from typing import List, Dict, Any

# ── ANSI color helpers ────────────────────────────────────────

# Enable VT100 on Windows
if os.name == "nt":
    os.system("")

_RESET = "\033[0m"
_BOLD  = "\033[1m"
_DIM   = "\033[2m"

_STATUS_STYLES = {
    "running":   "\033[1;32m",   # bold green
    "queued":    "\033[1;33m",   # bold yellow
    "completed": "\033[36m",     # cyan
    "failed":    "\033[1;31m",   # bold red
    "pending":   "\033[90m",     # gray
}

_STATUS_ICONS = {
    "running":   "●",
    "queued":    "◎",
    "completed": "✔",
    "failed":    "✖",
    "pending":   "○",
}


def _colored(text: str, style: str) -> str:
    return f"{style}{text}{_RESET}"


def _status_str(status: str) -> str:
    icon = _STATUS_ICONS.get(status, "?")
    style = _STATUS_STYLES.get(status, "")
    label = status.capitalize()
    return _colored(f"{icon} {label:<10}", style)


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _get_terminal_width() -> int:
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


# ── Public rendering functions ────────────────────────────────


def print_task_table(tasks: List[Dict[str, Any]], title: str = "Tasks") -> None:
    """Print a formatted task list table to stdout."""
    if not tasks:
        print(f"\n  {_DIM}No tasks found.{_RESET}\n")
        return

    tw = _get_terminal_width()
    # Column widths
    idx_w = max(3, len(str(len(tasks))))
    status_w = 13  # icon + label + padding
    time_w = 19    # "2026-03-04_22-30-00"
    # name gets the rest
    name_w = max(12, tw - idx_w - status_w - time_w - 10)

    # Header
    header = (
        f"  {_BOLD}{'#':<{idx_w}}  {'Status':<{status_w}} "
        f"{'Name':<{name_w}}  {'Created':<{time_w}}{_RESET}"
    )
    sep = f"  {'─' * (tw - 4)}"

    print(f"\n  {_BOLD}{title}{_RESET}  ({len(tasks)} total)")
    print(sep)
    print(header)
    print(sep)

    for i, t in enumerate(tasks, 1):
        name = _truncate(t.get("name", "unnamed"), name_w)
        created = t.get("created_at", "")[:time_w]
        status = t.get("status", "pending")
        status_cell = _status_str(status)

        print(f"  {i:<{idx_w}}  {status_cell} {name:<{name_w}}  {_DIM}{created}{_RESET}")

    print(sep)
    print()


def print_jobs(tasks: List[Dict[str, Any]]) -> None:
    """Print running/queued tasks in Linux ``jobs`` style.

    Example output::

        [1]+  Running    my-experiment_[1-of-3]
        [2]   Running    my-experiment_[2-of-3]
        [3]   Queued     baseline-lr-0.01
    """
    active = [t for t in tasks if t.get("status") in ("running", "queued")]
    if not active:
        print(f"\n  {_DIM}No active jobs.{_RESET}\n")
        return

    print()
    for i, t in enumerate(active, 1):
        status = t.get("status", "unknown").capitalize()
        name = t.get("name", "unnamed")
        style = _STATUS_STYLES.get(t.get("status", ""), "")
        marker = "+" if i == 1 else " "
        print(f"  [{i}]{marker}  {_colored(f'{status:<10}', style)}  {name}")
    print()


def print_task_detail(task: Dict[str, Any]) -> None:
    """Print a single task's detailed info."""
    tw = _get_terminal_width()
    sep = f"  {'─' * (tw - 4)}"

    print(f"\n  {_BOLD}{task.get('name', 'unnamed')}{_RESET}")
    print(sep)
    print(f"  Status:     {_status_str(task.get('status', 'pending'))}")
    print(f"  Created:    {task.get('created_at', 'N/A')}")
    print(f"  Directory:  {_DIM}{task.get('dir', 'N/A')}{_RESET}")

    starts = task.get("start_times", [])
    finishes = task.get("finish_times", [])
    if starts:
        print(f"  Runs:       {len(starts)}")
        print(f"  Last start: {starts[-1]}")
    if finishes:
        print(f"  Last end:   {finishes[-1]}")

    config = task.get("config", {})
    if config:
        from pyruns.utils.config_utils import preview_config_line
        preview = preview_config_line(config, max_items=8)
        if preview:
            print(f"  Config:     {_DIM}{preview}{_RESET}")

    print(sep)
    print()
