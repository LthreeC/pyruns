"""Reuse one lstat for existence and link metadata in the same check."""

import os
import sys

from pyruns.utils import info_io

_ORIGINAL_FILE = info_io.validate_workspace_file
_ORIGINAL_ROOT = info_io.validate_tasks_root


def _lstat_if_exists(path):
    try:
        return os.lstat(path)
    except (OSError, ValueError):
        return None


def validate_workspace_file(path, workspace_dir, *, label, _resolved_paths=None):
    absolute = os.path.abspath(path)
    root = os.path.abspath(workspace_dir)
    info_io.validate_workspace_directory(root)
    info = _lstat_if_exists(absolute)
    if info_io._stat_is_link_or_reparse(info):
        raise ValueError(f"{label} must not be a symlink, junction, or reparse point: {path}")
    if not info_io._path_is_within(absolute, root, _resolved_paths=_resolved_paths):
        raise ValueError(f"{label} resolves outside its workspace boundary: {path}")
    if info is None:
        return
    if not os.path.isfile(absolute):
        raise ValueError(f"{label} must be a regular file: {path}")


def validate_tasks_root(tasks_dir):
    absolute = os.path.abspath(tasks_dir)
    if info_io._stat_is_link_or_reparse(_lstat_if_exists(absolute)):
        raise ValueError(
            f"Tasks directory must not be a symlink, junction, or reparse point: {tasks_dir}"
        )
    info_io.validate_workspace_directory(os.path.dirname(absolute))


def install(enabled=True):
    for module in list(sys.modules.values()):
        if module is None or not getattr(module, "__name__", "").startswith("pyruns"):
            continue
        for name, value in list(vars(module).items()):
            if value is _ORIGINAL_FILE or value is validate_workspace_file:
                setattr(module, name, validate_workspace_file if enabled else _ORIGINAL_FILE)
            elif value is _ORIGINAL_ROOT or value is validate_tasks_root:
                setattr(module, name, validate_tasks_root if enabled else _ORIGINAL_ROOT)
