"""Shared task sorting and filtering helpers."""

import re
from typing import Dict, List

import yaml

_ACTIVE_STATUSES = {"running", "queued"}
_INACTIVE_TIE_PRIORITIES = {
    "failed": 3,
    "completed": 2,
    "pending": 1,
}
_NON_DIGIT_PATTERN = re.compile(r"\D+")
_NATURAL_CHUNK_PATTERN = re.compile(r"(\d+)")
_COLON_SPACES_PATTERN = re.compile(r"\s*:\s*")


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

    digits = _NON_DIGIT_PATTERN.sub("", str(timestamp))
    return int(digits) if digits else 0


def task_sort_key(task: Dict[str, object]) -> tuple:
    """Sort active tasks first, then by latest activity, then by status priority."""
    status = str(task.get("status", "pending") or "pending")
    active_rank = 1 if status in _ACTIVE_STATUSES else 0
    time_rank = _timestamp_weight(task)
    inactive_tie = _INACTIVE_TIE_PRIORITIES.get(status, 0)
    return (active_rank, time_rank, inactive_tie)


def _natural_name_key(value: object) -> tuple:
    chunks = _NATURAL_CHUNK_PATTERN.split(str(value or ""))
    return tuple(
        (1, int(chunk)) if chunk.isdigit() else (0, chunk.lower())
        for chunk in chunks
        if chunk
    )


def task_manager_sort_key(task: Dict[str, object]) -> tuple:
    """Sort one task by the Manager page's logical order within its pin group."""
    active_rank, time_rank, inactive_tie = task_sort_key(task)

    order = task.get("task_order")
    order_group = 0
    order_rank = -time_rank
    if order is not None:
        try:
            order_group = 1
            order_rank = float(order)
        except (TypeError, ValueError):
            pass

    return (
        -active_rank,
        order_group,
        order_rank,
        -inactive_tie,
        -time_rank,
        _natural_name_key(task.get("name", "")),
    )


def sort_tasks_for_manager(tasks: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """Return pinned tasks first, then active/fresh tasks ahead of old manual order."""
    valid = [task for task in tasks if task is not None]
    pinned = sorted(
        [task for task in valid if task.get("pinned")],
        key=task_manager_sort_key,
    )
    others = sorted(
        [task for task in valid if not task.get("pinned")],
        key=task_manager_sort_key,
    )
    return pinned + others


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
        normalized_blob = str(task.get("search_text", "") or "").lower()
        if not normalized_blob:
            try:
                yaml_str = yaml.dump(task.get("config", {}), default_flow_style=False).lower()
            except Exception:
                yaml_str = str(task.get("config", {})).lower()
            text_blob = f"{task.get('name', '')}\n{yaml_str}\n{task.get('notes', '')}".lower()
            normalized_blob = text_blob
        normalized_blob = _COLON_SPACES_PATTERN.sub(":", normalized_blob)

        for line in query_lines:
            normalized_line = _COLON_SPACES_PATTERN.sub(":", line)
            if normalized_line not in normalized_blob:
                return False
        return True

    return [task for task in tasks if matches_all(task)]
