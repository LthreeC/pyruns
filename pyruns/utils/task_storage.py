"""Resolve the common and device-specific task stores within one workspace."""

import os
import re
import subprocess

from pyruns._config import TASKS_DIR, TASK_INFO_FILENAME
from pyruns.utils.process_utils import hidden_subprocess_kwargs
from pyruns.utils.runner_device import device_tasks_folder


_DEVICE_FOLDER = re.compile(r"tasks-([a-z0-9._-]{1,48})-([0-9a-f]{12})")


def discover_device_roots(tasks_dir: str) -> tuple[str, ...]:
    """Discover sibling device stores without descending into project directories."""
    if os.path.basename(os.path.normpath(tasks_dir)) != TASKS_DIR:
        return ()
    workspace = os.path.dirname(tasks_dir) or "."
    local = device_tasks_folder()
    with os.scandir(workspace) as entries:
        return tuple(sorted(entry.path for entry in entries if entry.name != local
                            and _DEVICE_FOLDER.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)))


def task_source(task_dir: str) -> str:
    """Return the stable filter key; a custom legacy tasks root is shared."""
    folder = os.path.basename(os.path.dirname(task_dir))
    if folder == device_tasks_folder():
        return "local"
    return folder if _DEVICE_FOLDER.fullmatch(folder) else "shared"


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
    source = task_source(task_dir)
    return source if source in ("local", "shared") else "remote"
