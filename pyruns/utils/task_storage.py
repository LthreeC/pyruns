"""Resolve the common and device-specific task stores within one workspace."""

import os
import subprocess

from pyruns._config import TASKS_DIR, TASK_INFO_FILENAME
from pyruns.utils.process_utils import hidden_subprocess_kwargs
from pyruns.utils.runner_device import device_tasks_folder


def git_tracks_task(task_dir: str) -> bool:
    """Check ambiguous mutations in a worktree without taking a Git index lock."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "ls-files", "--error-unmatch", "--", TASK_INFO_FILENAME],
            cwd=task_dir, timeout=2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    return result.returncode != 1  # Unknown Git errors also leave the snapshot alone.


def visible_task_roots(tasks_dir: str) -> tuple[str, ...]:
    directory = os.path.basename(os.path.normpath(tasks_dir))
    if directory not in {TASKS_DIR, device_tasks_folder()}:
        return (tasks_dir,)
    workspace = os.path.dirname(tasks_dir)
    return os.path.join(workspace, TASKS_DIR), os.path.join(workspace, device_tasks_folder())


def new_task_root(tasks_dir: str, storage: str) -> str:
    if storage not in ("shared", "local"):
        raise ValueError("task_storage must be shared or local")
    roots = visible_task_roots(tasks_dir)
    return roots[-1] if storage == "local" else roots[0]


def task_storage_scope(task_dir: str) -> str:
    return "local" if os.path.basename(os.path.dirname(task_dir)) == device_tasks_folder() else "shared"
