"""
Shared task sorting — used by both Manager and Monitor pages.

Sort key: latest activity timestamp (newest first).
Priority: run_at[-1] > created_at.
"""
from typing import Any, Dict


def task_sort_key(task: Dict[str, Any]) -> str:
    """Return the most recent activity timestamp for sorting.

    - Use the last ``start_times`` entry (most recent run).
    - Fall back to ``created_at``.
    """
    starts = task.get("start_times") or []
    if isinstance(starts, list) and starts:
        return starts[-1]
    return task.get("created_at") or ""
