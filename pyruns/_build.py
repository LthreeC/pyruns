"""Setuptools commands used only while building Pyruns distributions."""

from __future__ import annotations

import os
import shutil
import stat
import time
from pathlib import Path

from setuptools.command.build_py import build_py


_CLEAN_RETRY_COUNT = 5
_CLEAN_RETRY_DELAY_SEC = 0.05


def _retry_readonly_removal(operation, path: str, _error) -> None:
    """Clear a Windows read-only bit and retry one failed removal."""

    os.chmod(path, stat.S_IWRITE)
    operation(path)


def _remove_build_path(path: Path) -> None:
    for attempt in range(_CLEAN_RETRY_COUNT):
        try:
            if path.is_symlink():
                path.unlink()
            elif path.is_file():
                path.chmod(stat.S_IWRITE)
                path.unlink()
            elif path.exists():
                shutil.rmtree(path, onerror=_retry_readonly_removal)
            return
        except OSError:
            if attempt >= _CLEAN_RETRY_COUNT - 1:
                raise
            time.sleep(_CLEAN_RETRY_DELAY_SEC * (attempt + 1))


class CleanBuildPy(build_py):
    """Build Python files after removing stale output for the Pyruns package."""

    def run(self) -> None:
        build_root = Path(self.build_lib).resolve()
        package_build_dir = build_root / "pyruns"
        resolved_target = package_build_dir.resolve()
        source_package_dir = Path(self.get_package_dir("pyruns")).resolve()
        if (
            resolved_target == source_package_dir
            or resolved_target in source_package_dir.parents
            or source_package_dir in resolved_target.parents
        ):
            raise RuntimeError("refusing to clean a build directory that overlaps Pyruns source")

        _remove_build_path(package_build_dir)
        super().run()
