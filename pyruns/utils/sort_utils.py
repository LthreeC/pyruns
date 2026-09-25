"""Shared task sorting and filtering helpers."""

import math
import re
from collections.abc import Iterable, Mapping
from typing import List, TypeVar

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
_Task = TypeVar("_Task", bound=Mapping[str, object])


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


def _timestamp_weight(task: Mapping[str, object]) -> int:
    """Convert the latest task activity timestamp to a sortable integer."""
    finishes = task.get("finish_times") or []
    starts = task.get("start_times") or []

    if isinstance(finishes, (list, tuple)) and finishes:
        timestamp = finishes[-1]
    elif isinstance(starts, (list, tuple)) and starts:
        timestamp = starts[-1]
    else:
        timestamp = task.get("created_at") or ""

    digits = _NON_DIGIT_PATTERN.sub("", str(timestamp))
    # Real timestamps are short. Ignore corrupt values before integer parsing,
    # whose limit varies between Python versions and interpreter settings.
    if len(digits) > 64:
        return 0
    return int(digits) if digits else 0


def task_sort_key(task: Mapping[str, object]) -> tuple:
    """Sort active tasks first, then by latest activity, then by status priority."""
    status = str(task.get("status", "pending") or "pending")
    active_rank = 1 if status in _ACTIVE_STATUSES else 0
    time_rank = _timestamp_weight(task)
    inactive_tie = _INACTIVE_TIE_PRIORITIES.get(status, 0)
    return (active_rank, time_rank, inactive_tie)


def _natural_name_key(value: object) -> tuple:
    # Flat kind/value pairs preserve natural ordering without retaining a
    # separate tuple for every chunk until the entire task sort finishes.
    key = []
    for chunk in _NATURAL_CHUNK_PATTERN.split(str(value or "")):
        if chunk:
            key.extend((1, int(chunk)) if chunk.isdecimal() else (0, chunk.lower()))
    return tuple(key)


def _finite_task_order(value: object) -> float | None:
    """Treat malformed saved weights as unset, preserving a total ordering."""
    if value is None:
        return None
    try:
        order = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return order if math.isfinite(order) else None


def task_manager_sort_key(task: Mapping[str, object]) -> tuple:
    """Sort one task by the Manager page's logical order within its pin group."""
    active_rank, time_rank, inactive_tie = task_sort_key(task)

    order = _finite_task_order(task.get("task_order"))
    order_group = 0
    order_rank = -time_rank
    if order is not None:
        order_group = 1
        order_rank = order

    return (
        -active_rank,
        order_group,
        order_rank,
        -inactive_tie,
        -time_rank,
        *_natural_name_key(task.get("name", "")),
    )


def _manual_sort_key(task: Mapping[str, object]) -> tuple:
    order = _finite_task_order(task.get("task_order"))
    if order is not None:
        return (0, order, *_natural_name_key(task.get("name", "")))
    return (1, *task_manager_sort_key(task))


def _sort_manager_group(
    tasks: list[_Task],
    sort_mode: str,
) -> list[_Task]:
    if sort_mode == "priority":
        return sorted(tasks, key=task_manager_sort_key)
    if sort_mode == "manual":
        return sorted(tasks, key=_manual_sort_key)
    if sort_mode == "activity_desc":
        return sorted(
            tasks,
            key=lambda task: (-_timestamp_weight(task), *_natural_name_key(task.get("name", ""))),
        )
    if sort_mode == "activity_asc":
        return sorted(
            tasks,
            key=lambda task: (_timestamp_weight(task), *_natural_name_key(task.get("name", ""))),
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
    tasks: Iterable[_Task | None],
    sort_mode: str = "priority",
) -> list[_Task]:
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


def filter_tasks(all_tasks: Iterable[_Task], query: str, status_mode: str = "All") -> list[_Task]:
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

    def matches_all(task: Mapping[str, object]) -> bool:
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
