"""
Task-level I/O utilities — reading task_info, log files, monitor data.

These are low-level data access helpers used by both core/ and ui/ layers.
They contain NO business logic (no status transitions, no scheduling, etc.).
"""
import os
import json
import re
from typing import Dict, Any, List, Optional

from pyruns._config import (
    INFO_FILENAME,
    RUN_LOG_DIR,
)


def load_task_info(task_dir: str, raise_error: bool = False) -> Dict[str, Any]:
    """Load task_info.json from a task directory."""
    info_path = os.path.join(task_dir, INFO_FILENAME)
    if not os.path.exists(info_path):
        return {}
    try:
        with open(info_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        if raise_error:
            raise
        return {}


def save_task_info(task_dir: str, info: Dict[str, Any]) -> None:
    """Save task_info.json to a task directory."""
    info_path = os.path.join(task_dir, INFO_FILENAME)
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2, ensure_ascii=False)


def load_monitor_data(task_dir: str) -> List[Dict[str, Any]]:
    """Load monitor entries from task_info.json."""
    info = load_task_info(task_dir)
    return info.get("monitors", [])


def get_log_options(task_dir: str) -> Dict[str, str]:
    """Return {display_name: file_path} for all available log files.

    Scans the new ``run_logs/`` directory for ``runN.log`` files.
    """
    opts: Dict[str, str] = {}

    # ── New scheme: run_logs/run1.log, run2.log, … ──
    run_dir = os.path.join(task_dir, RUN_LOG_DIR)
    if os.path.isdir(run_dir):
        # 1. Standard run logs
        files = sorted(
            [f for f in os.listdir(run_dir)
             if f.startswith("run") and f.endswith(".log")],
            key=lambda x: int("".join(filter(str.isdigit, x)) or "0"),
        )
        for f in files:
            opts[f] = os.path.join(run_dir, f)
            
        # 2. Error log (if exists)
        from pyruns._config import ERROR_LOG_FILENAME
        err_path = os.path.join(run_dir, ERROR_LOG_FILENAME)
        if os.path.exists(err_path):
            opts[ERROR_LOG_FILENAME] = err_path

    return opts


def resolve_log_path(task_dir: str, log_file_name: Optional[str] = None) -> Optional[str]:
    """Resolve which log file to display for a task.

    If log_file_name is given, look it up. Otherwise pick the **latest**
    (highest numbered) log file.
    Returns the absolute file path, or None if no log exists.
    """
    opts = get_log_options(task_dir)
    if log_file_name:
        return opts.get(log_file_name)
    # Default: latest log based on modification time
    if opts:
        # Sort by mtime descending
        cached = [(f, p, os.path.getmtime(p)) for f, p in opts.items()]
        cached.sort(key=lambda x: x[2], reverse=True)
        return cached[0][1]
    return None



# Characters invalid in folder names (Windows + Unix)
_INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def validate_task_name(name: str, root_dir: Optional[str] = None) -> Optional[str]:
    """
    Validate whether a task name can be used as a folder name.
    Returns None if valid, or an error message string if invalid.
    """
    if not name or not name.strip():
        return "Task name cannot be empty"
    name = name.strip()
    if len(name) > 200:
        return "Task name is too long (max 200 characters)"
    bad = _INVALID_CHARS_RE.findall(name)
    if bad:
        return f"Task name contains invalid characters: {''.join(set(bad))}"
    if name.startswith("."):
        return "Task name cannot start with '.'"
    
    if root_dir:
        if os.path.exists(os.path.join(root_dir, name)):
            return f"Task name '{name}' already exists in the current workspace"
            
    return None
