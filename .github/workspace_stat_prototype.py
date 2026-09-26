"""Prototype: use the last ancestor lstat for workspace-directory type checks."""
import os
import stat

from pyruns.utils import info_io


def is_reparse(info):
    return info is not None and (
        stat.S_ISLNK(info.st_mode)
        or bool(int(getattr(info, "st_file_attributes", 0) or 0)
                & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))
    )


def validate_workspace_directory(workspace_dir):
    absolute = os.path.abspath(workspace_dir)
    info = None
    for current in info_io._managed_ancestor_paths(absolute):
        try:
            info = os.lstat(current)
        except OSError:
            info = None
        if is_reparse(info):
            raise ValueError(
                "Managed workspace path must not contain a symlink, junction, "
                f"or reparse point: {current}"
            )
    if info is None:
        try:
            info = os.lstat(absolute)
        except (OSError, ValueError):
            return
    if is_reparse(info):
        raise ValueError(
            "Workspace directory must not be a symlink, junction, "
            f"or reparse point: {workspace_dir}"
        )
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Workspace path must be a directory: {workspace_dir}")
