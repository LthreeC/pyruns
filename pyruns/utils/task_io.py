"""
Task-level I/O utilities — reading task_info, log files, monitor data.

These are low-level data access helpers used by both core/ and ui/ layers.
They contain NO business logic (no status transitions, no scheduling, etc.).
"""
import os
import json
from typing import Dict, Any, List, Optional

from pyruns._config import INFO_FILENAME, MONITOR_KEY, LOG_FILENAME, RERUN_LOG_DIR


def load_task_info(task_dir: str) -> Dict[str, Any]:
    """Load task_info.json from a task directory."""
    info_path = os.path.join(task_dir, INFO_FILENAME)
    if not os.path.exists(info_path):
        return {}
    try:
        with open(info_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_task_info(task_dir: str, info: Dict[str, Any]) -> None:
    """Save task_info.json to a task directory."""
    info_path = os.path.join(task_dir, INFO_FILENAME)
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2, ensure_ascii=False)


def load_monitor_data(task_dir: str) -> List[Dict[str, Any]]:
    """Load monitor entries from task_info.json."""
    info = load_task_info(task_dir)
    return info.get(MONITOR_KEY, [])


def get_log_options(task_dir: str) -> Dict[str, str]:
    """Return {display_name: file_path} for run.log + rerunX.log."""
    opts: Dict[str, str] = {}
    run_log = os.path.join(task_dir, LOG_FILENAME)
    if os.path.exists(run_log):
        opts["run.log"] = run_log
    rerun_dir = os.path.join(task_dir, RERUN_LOG_DIR)
    if os.path.isdir(rerun_dir):
        files = sorted(
            [f for f in os.listdir(rerun_dir) if f.startswith("rerun") and f.endswith(".log")],
            key=lambda x: int("".join(filter(str.isdigit, x)) or "0"),
        )
        for f in files:
            opts[f] = os.path.join(rerun_dir, f)
    return opts


def resolve_log_path(task_dir: str, log_file_name: Optional[str] = None) -> Optional[str]:
    """Resolve which log file to display for a task.

    If log_file_name is given, look it up. Otherwise pick the first available.
    Returns the absolute file path, or None if no log exists.
    """
    opts = get_log_options(task_dir)
    name = log_file_name or (list(opts.keys())[0] if opts else None)
    return opts.get(name) if name else None

