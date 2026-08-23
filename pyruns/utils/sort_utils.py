"""Shared task sorting and filtering helpers."""

import math
import re
from typing import Dict, List

from omegaconf import OmegaConf

_ACTIVE_STATUSES = {"running", "queued"}
_INACTIVE_TIE_PRIORITIES = {
    "failed": 3,
    "completed": 2,
    "pending": 1,
}
_NON_DIGIT_PATTERN = re.compile(r"\D+")
_NATURAL_CHUNK_PATTERN = re.compile(r"(\d+)")
_COLON_SPACES_PATTERN = re.compile(r"[^\S\r\n]*:[^\S\r\n]*")
TASK_SORT_MODES = frozenset({
    "priority",
    "manual",
    "activity_desc",
    "activity_asc",
    "name_asc",
    "name_desc",
})


def normalize_task_search_text(value: object) -> str:
    """Normalize task search text without collapsing line boundaries."""

    return _COLON_SPACES_PATTERN.sub(":", str(value or "").lower())


def task_search_needles(query: str) -> List[str]:
    """Return the non-empty normalized lines used by task search."""

    needles: List[str] = []
    for line in str(query or "").split("\n"):
        if not line.strip():
            continue
        normalized = normalize_task_search_text(line.strip())
        if normalized not in needles:
            needles.append(normalized)
    return needles


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


def _manual_sort_key(task: Dict[str, object]) -> tuple:
    order = task.get("task_order")
    try:
        normalized_order = float(order) if order is not None else math.nan
    except (TypeError, ValueError):
        normalized_order = math.nan
    if math.isfinite(normalized_order):
        return (0, normalized_order, _natural_name_key(task.get("name", "")))
    return (1, *task_manager_sort_key(task))


def _sort_manager_group(
    tasks: List[Dict[str, object]],
    sort_mode: str,
) -> List[Dict[str, object]]:
    if sort_mode == "priority":
        return sorted(tasks, key=task_manager_sort_key)
    if sort_mode == "manual":
        return sorted(tasks, key=_manual_sort_key)
    if sort_mode == "activity_desc":
        return sorted(
            tasks,
            key=lambda task: (-_timestamp_weight(task), _natural_name_key(task.get("name", ""))),
        )
    if sort_mode == "activity_asc":
        return sorted(
            tasks,
            key=lambda task: (_timestamp_weight(task), _natural_name_key(task.get("name", ""))),
        )
    if sort_mode == "name_asc":
        return sorted(tasks, key=lambda task: _natural_name_key(task.get("name", "")))
    if sort_mode == "name_desc":
        return sorted(
            tasks,
            key=lambda task: _natural_name_key(task.get("name", "")),
            reverse=True,
        )
    raise ValueError(f"Unknown task sort mode: {sort_mode}")


def sort_tasks_for_manager(
    tasks: List[Dict[str, object]],
    sort_mode: str = "priority",
) -> List[Dict[str, object]]:
    """Sort Manager cards within pinned and unpinned groups."""
    if sort_mode not in TASK_SORT_MODES:
        raise ValueError(f"Unknown task sort mode: {sort_mode}")
    valid = [task for task in tasks if task is not None]
    pinned = _sort_manager_group(
        [task for task in valid if task.get("pinned")],
        sort_mode,
    )
    others = _sort_manager_group(
        [task for task in valid if not task.get("pinned")],
        sort_mode,
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

    query_lines = task_search_needles(query)
    if not query_lines:
        return tasks

    def matches_all(task: Dict[str, object]) -> bool:
        normalized_blob = normalize_task_search_text(task.get("search_text", ""))
        if not normalized_blob:
            try:
                yaml_str = OmegaConf.to_yaml(
                    OmegaConf.create(task.get("config", {}) or {}),
                    resolve=False,
                ).lower()
            except Exception:
                yaml_str = str(task.get("config", {})).lower()
            text_blob = f"{task.get('name', '')}\n{yaml_str}\n{task.get('notes', '')}".lower()
            normalized_blob = normalize_task_search_text(text_blob)

        for line in query_lines:
            if line not in normalized_blob:
                return False
        return True

    return [task for task in tasks if matches_all(task)]
