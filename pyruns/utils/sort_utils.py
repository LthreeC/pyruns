"""Shared task sorting and filtering helpers."""

import re
from typing import Dict

import yaml

_ACTIVE_STATUSES = {"running", "queued"}
_INACTIVE_TIE_PRIORITIES = {
    "failed": 3,
    "completed": 2,
    "pending": 1,
}


def _timestamp_weight(task: Dict[str, object]) -> int:
    """Convert the latest task activity timestamp to a sortable integer."""
    finishes = task.get("finish_times") or []
    starts = task.get("start_times") or []

    if isinstance(finishes, list) and finishes:
        timestamp = finishes[-1]
    elif isinstance(starts, list) and starts:
        timestamp = starts[-1]
    else:
        timestamp = task.get("created_at") or ""

    digits = "".join(ch for ch in str(timestamp) if ch.isdigit())
    return int(digits) if digits else 0


def task_sort_key(task: Dict[str, object]) -> tuple:
    """Sort active tasks first, then by latest activity, then by status priority."""
    status = str(task.get("status", "pending") or "pending")
    active_rank = 1 if status in _ACTIVE_STATUSES else 0
    time_rank = _timestamp_weight(task)
    inactive_tie = _INACTIVE_TIE_PRIORITIES.get(status, 0)
    return (active_rank, time_rank, inactive_tie)


def filter_tasks(all_tasks: list, query: str, status_mode: str = "All") -> list:
    """Apply status and multiline deep-search filtering."""
    tasks = [
        task for task in all_tasks
        if status_mode == "All" or status_mode.lower() == task.get("status", "")
    ]
    if not query:
        return tasks

    query_lines = [line.strip().lower() for line in query.split("\n") if line.strip()]
    if not query_lines:
        return tasks

    def matches_all(task: Dict[str, object]) -> bool:
        try:
            yaml_str = yaml.dump(task.get("config", {}), default_flow_style=False).lower()
        except Exception:
            yaml_str = str(task.get("config", {})).lower()

        notes_str = str(task.get("notes", "")).lower()
        text_blob = f"{task.get('name', '').lower()}\n{yaml_str}\n{notes_str}"
        normalized_blob = re.sub(r"\s*:\s*", ":", text_blob)

        for line in query_lines:
            normalized_line = re.sub(r"\s*:\s*", ":", line)
            if normalized_line not in normalized_blob:
                return False
        return True

    return [task for task in tasks if matches_all(task)]
