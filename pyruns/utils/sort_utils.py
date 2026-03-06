"""
Shared task sorting — used by both Manager and Monitor pages.

Sort key: latest activity timestamp (newest first).
Priority: run_at[-1] > created_at.
"""
import re
import yaml
from typing import Any, Dict

_ACTIVE_STATUSES = {"running", "queued"}
_INACTIVE_TIE_PRIORITIES = {
    "failed": 3,
    "completed": 2,
    "pending": 1,
}


def _timestamp_weight(task: Dict[str, Any]) -> int:
    """Convert task activity timestamp to sortable int YYYYMMDDhhmmss[us]."""
    finishes = task.get("finish_times") or []
    starts = task.get("start_times") or []

    if isinstance(finishes, list) and finishes:
        ts = finishes[-1]
    elif isinstance(starts, list) and starts:
        ts = starts[-1]
    else:
        ts = task.get("created_at") or ""

    digits = "".join(ch for ch in str(ts) if ch.isdigit())
    return int(digits) if digits else 0


def task_sort_key(task: Dict[str, Any]) -> tuple:
    """Return a tuple for intuitive sorting with ``reverse=True``.

    Rules:
      1. Active tasks (running/queued) always first.
      2. Inside each group, latest activity first.
      3. Inactive ties are broken by failed > completed > pending.
    """
    status = str(task.get("status", "pending") or "pending")
    active_rank = 1 if status in _ACTIVE_STATUSES else 0
    time_rank = _timestamp_weight(task)
    inactive_tie = _INACTIVE_TIE_PRIORITIES.get(status, 0)
    return (active_rank, time_rank, inactive_tie)


def filter_tasks(all_tasks: list, query: str, status_mode: str = "All") -> list:
    """Apply status + deep search filters.
    
    Searches both task name and the serialized configuration dictionary.
    """
    tasks = [
        t for t in all_tasks
        if status_mode == "All" or status_mode.lower() == t.get("status", "")
    ]
    if query:
        # User may paste multi-line YAML blocks:
        # device: null
        # batch_size: 32
        # We ensure EVERY non-empty line exists in the task's YAML representation.

        query_lines = [line.strip().lower() for line in query.split('\n') if line.strip()]
        if query_lines:
            def matches_all(t):
                # Dump config to yaml to match standard YAML strings like 'device: null'
                try:
                    yaml_str = yaml.dump(t.get("config", {}), default_flow_style=False).lower()
                except Exception:
                    yaml_str = str(t.get("config", {})).lower()

                notes_str = str(t.get("notes", "")).lower()
                text_blob = t.get("name", "").lower() + "\n" + yaml_str + "\n" + notes_str
                # For robust matching, remove all spaces around colons in both texts
                # so "device: null" matches "device:null" etc.

                blob_norm = re.sub(r'\s*:\s*', ':', text_blob)
                
                for q_line in query_lines:
                    q_norm = re.sub(r'\s*:\s*', ':', q_line)
                    if q_norm not in blob_norm:
                        return False
                return True
                
            tasks = [t for t in tasks if matches_all(t)]
    return tasks

