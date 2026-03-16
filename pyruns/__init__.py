"""Pyruns - lightweight Python experiment management."""

from __future__ import annotations

import os
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Dict, Optional

from ._config import (
    CONFIG_DEFAULT_FILENAME,
    ENV_KEY_CONFIG,
    RECORDS_KEY,
    ROOT_DIR,
    TRACKS_KEY,
)
from .core.config_manager import ConfigManager
from .utils.info_io import ensure_run_slot, load_task_info, run_slot_count, update_task_info

try:
    __version__ = version("pyruns")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"


_global_config_manager_ = ConfigManager()


def _get_default_config_path() -> str:
    script_path = sys.argv[0] if sys.argv else ""
    if script_path and os.path.isfile(script_path):
        script_base = os.path.splitext(os.path.basename(script_path))[0]
        return os.path.join(ROOT_DIR, script_base, CONFIG_DEFAULT_FILENAME)
    raise FileNotFoundError(
        f"Default config path cannot be determined because script path is invalid: {script_path}"
    )


def read(file_path: str = None):
    """Read a config file into the global config manager."""
    pyr_config = os.environ.get(ENV_KEY_CONFIG)
    if pyr_config:
        return _global_config_manager_.read(pyr_config)

    if not file_path:
        file_path = _get_default_config_path()

    if not os.path.exists(file_path) and not os.environ.get(ENV_KEY_CONFIG):
        from ._config import DEFAULT_ROOT_NAME

        script_name = os.path.basename(sys.argv[0]) if sys.argv else "script.py"
        script_base = os.path.splitext(script_name)[0]
        print(
            f"\n\033[93m[pyruns] Config not found: {file_path}\033[0m\n"
            f"You can either:\n"
            f"  1. Manually create the config file at {DEFAULT_ROOT_NAME}/{script_base}/{CONFIG_DEFAULT_FILENAME}\n"
            f"  2. Or use CLI to import one: `pyr {script_name} your_config.yaml`\n"
        )

    return _global_config_manager_.read(file_path)


def load():
    """Return the loaded config, auto-reading it when needed."""
    if _global_config_manager_._root is None:
        pyr_config = os.environ.get(ENV_KEY_CONFIG)
        if pyr_config:
            _global_config_manager_.read(pyr_config)
        else:
            default_path = _get_default_config_path()
            if os.path.exists(default_path):
                _global_config_manager_.read(default_path)
            else:
                from ._config import DEFAULT_ROOT_NAME

                script_name = os.path.basename(sys.argv[0]) if sys.argv else "script.py"
                script_base = os.path.splitext(script_name)[0]
                print(
                    f"\n\033[93m[pyruns] Config not found: {default_path}\033[0m\n"
                    f"You can either:\n"
                    f"  1. Manually create the config file at {DEFAULT_ROOT_NAME}/{script_base}/{CONFIG_DEFAULT_FILENAME}\n"
                    f"  2. Or use CLI to import one: `pyr {script_name} your_config.yaml`\n"
                )

    return _global_config_manager_.load()


def ensure_config_default(root_dir: str = None):
    """Create ``config_default.yaml`` with defaults if it doesn't exist."""
    if root_dir is None:
        root_dir = ROOT_DIR
    path = os.path.join(root_dir, CONFIG_DEFAULT_FILENAME)
    if not os.path.exists(path):
        os.makedirs(root_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# task config here")
    return path


def record(data: Optional[Dict[str, Any]] = None, **kwargs) -> None:
    """Append or merge record data into the current task's ``records`` slot."""
    if data is not None and not isinstance(data, dict):
        raise TypeError("record expects a dict or keyword arguments")

    pyr_config = os.environ.get(ENV_KEY_CONFIG)
    if not pyr_config:
        return

    update_data: Dict[str, Any] = {}
    if data:
        update_data.update(data)
    update_data.update(kwargs)
    if not update_data:
        return

    task_dir = os.path.dirname(pyr_config)
    for _attempt in range(5):
        try:
            run_index_str = os.environ.get("PYRUNS_RUN_INDEX")
            if run_index_str and run_index_str.isdigit():
                run_index = int(run_index_str)
            else:
                info = load_task_info(task_dir, raise_error=True)
                run_index = max(1, run_slot_count(info))

            def _apply(info: Dict[str, Any]) -> None:
                slot = ensure_run_slot(info, run_index)
                info[RECORDS_KEY][slot].update(update_data)

            update_task_info(task_dir, _apply, raise_error=True)
            return
        except (IOError, OSError):
            time.sleep(0.05)


def track(key: Optional[str] = None, value: Any = None, **kwargs) -> None:
    """Append time-series track data into the current task's ``tracks`` slot."""
    pyr_config = os.environ.get(ENV_KEY_CONFIG)
    if not pyr_config:
        return

    update_data = {}
    if key is not None and value is not None:
        update_data[key] = value
    update_data.update(kwargs)
    if not update_data:
        return

    task_dir = os.path.dirname(pyr_config)
    for _attempt in range(5):
        try:
            run_index_str = os.environ.get("PYRUNS_RUN_INDEX")
            if run_index_str and run_index_str.isdigit():
                run_index = int(run_index_str)
            else:
                info = load_task_info(task_dir, raise_error=True)
                run_index = max(1, run_slot_count(info))

            def _apply(info: Dict[str, Any]) -> None:
                slot = ensure_run_slot(info, run_index)
                current_tracks = info[TRACKS_KEY][slot]
                for item_key, item_value in update_data.items():
                    current_tracks.setdefault(item_key, []).append(item_value)

            update_task_info(task_dir, _apply, raise_error=True)
            return
        except (IOError, OSError):
            time.sleep(0.05)


def get_task_dir() -> Optional[str]:
    """Return the current task directory, or ``None`` outside pyruns."""
    pyr_config = os.environ.get(ENV_KEY_CONFIG)
    if not pyr_config:
        return None
    return os.path.dirname(pyr_config)


def get_run_index() -> Optional[int]:
    """Return the current run index, or ``None`` outside pyruns."""
    pyr_config = os.environ.get(ENV_KEY_CONFIG)
    if not pyr_config:
        return None
    info = load_task_info(os.path.dirname(pyr_config), raise_error=True)
    return run_slot_count(info)
