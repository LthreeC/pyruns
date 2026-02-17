"""
Shared task sorting — used by both Manager and Monitor pages.

Sort key: latest activity timestamp (newest first).
Priority: rerun_at[-1] > run_at > created_at.
"""
from typing import Any, Dict


def task_sort_key(task: Dict[str, Any]) -> str:
    """Return the most recent activity timestamp for sorting.

    - If the task has been re-run, use the last ``rerun_at`` entry.
    - Otherwise use ``run_at``.
    - Fall back to ``created_at``.
    """
    rerun_at = task.get("rerun_at") or []
    if rerun_at:
        return rerun_at[-1]
    if task.get("run_at"):
        return task["run_at"]
    return task.get("created_at") or ""
