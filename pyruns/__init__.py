"""Pyruns - lightweight Python experiment management."""

from __future__ import annotations

import os
import sys
import time
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from threading import Lock
from typing import TYPE_CHECKING, Any, Dict, Optional

from ._config import (
    ARTIFACTS_DIR,
    CONFIG_DEFAULT_FILENAME,
    ENV_KEY_CONFIG,
    ENV_KEY_RUN_INDEX,
    RECORDS_KEY,
    ROOT_DIR,
    TRACKS_KEY,
)

__all__ = [
    "ARTIFACTS_DIR",
    "CONFIG_DEFAULT_FILENAME",
    "ConfigManager",
    "ENV_KEY_CONFIG",
    "ENV_KEY_RUN_INDEX",
    "RECORDS_KEY",
    "ROOT_DIR",
    "TRACKS_KEY",
    "__version__",
    "artifact_dir",
    "ensure_config_default",
    "ensure_run_slot",
    "get_artifact_dir",
    "get_run_index",
    "get_task_dir",
    "load",
    "load_task_info",
    "read",
    "record",
    "run_slot_count",
    "track",
    "update_task_info",
]

if TYPE_CHECKING:
    from .core.config_manager import ConfigManager

try:
    __version__ = version("pyruns")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"


_LAZY_EXPORTS = {
    "ConfigManager": (".core.config_manager", "ConfigManager"),
    "ensure_run_slot": (".utils.info_io", "ensure_run_slot"),
    "load_task_info": (".utils.info_io", "load_task_info"),
    "run_slot_count": (".utils.info_io", "run_slot_count"),
    "update_task_info": (".utils.info_io", "update_task_info"),
}

_global_config_manager_: Optional["ConfigManager"] = None
_config_manager_lock = Lock()


def __getattr__(name: str) -> Any:
    """Load compatibility exports only when callers request them."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


def _lazy_export(name: str) -> Any:
    """Resolve a lazy export while honoring callers that monkeypatch it."""
    try:
        return globals()[name]
    except KeyError:
        return __getattr__(name)


def _get_config_manager() -> "ConfigManager":
    global _global_config_manager_
    if _global_config_manager_ is None:
        with _config_manager_lock:
            if _global_config_manager_ is None:
                manager_type = _lazy_export("ConfigManager")
                _global_config_manager_ = manager_type()
    return _global_config_manager_


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
    config_manager = _get_config_manager()
    pyr_config = os.environ.get(ENV_KEY_CONFIG)
    if pyr_config:
        return config_manager.read(pyr_config)

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
            f"  2. Or import one with: `pyr init {script_name} --config your_config.yaml`\n"
        )

    return config_manager.read(file_path)


def load():
    """Return the loaded config, auto-reading it when needed."""
    config_manager = _get_config_manager()
    if config_manager._root is None:
        pyr_config = os.environ.get(ENV_KEY_CONFIG)
        if pyr_config:
            config_manager.read(pyr_config)
        else:
            default_path = _get_default_config_path()
            if os.path.exists(default_path):
                config_manager.read(default_path)
            else:
                from ._config import DEFAULT_ROOT_NAME

                script_name = os.path.basename(sys.argv[0]) if sys.argv else "script.py"
                script_base = os.path.splitext(script_name)[0]
                print(
                    f"\n\033[93m[pyruns] Config not found: {default_path}\033[0m\n"
                    f"You can either:\n"
                    f"  1. Manually create the config file at {DEFAULT_ROOT_NAME}/{script_base}/{CONFIG_DEFAULT_FILENAME}\n"
                    f"  2. Or import one with: `pyr init {script_name} --config your_config.yaml`\n"
                )

    return config_manager.load()


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


def _get_env_run_index() -> Optional[int]:
    raw = str(os.environ.get(ENV_KEY_RUN_INDEX, "") or "").strip()
    if not raw:
        return None
    try:
        run_index = int(raw)
    except ValueError:
        return None
    return run_index if run_index > 0 else None


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
            run_index = _get_env_run_index()
            if run_index is None:
                info = _lazy_export("load_task_info")(task_dir, raise_error=True)
                run_index = max(1, _lazy_export("run_slot_count")(info))

            def _apply(info: Dict[str, Any]) -> None:
                slot = _lazy_export("ensure_run_slot")(info, run_index)
                info[RECORDS_KEY][slot].update(update_data)

            _lazy_export("update_task_info")(task_dir, _apply, raise_error=True)
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
            run_index = _get_env_run_index()
            if run_index is None:
                info = _lazy_export("load_task_info")(task_dir, raise_error=True)
                run_index = max(1, _lazy_export("run_slot_count")(info))

            def _apply(info: Dict[str, Any]) -> None:
                slot = _lazy_export("ensure_run_slot")(info, run_index)
                current_tracks = info[TRACKS_KEY][slot]
                for item_key, item_value in update_data.items():
                    current_tracks.setdefault(item_key, []).append(item_value)

            _lazy_export("update_task_info")(task_dir, _apply, raise_error=True)
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
    env_run_index = _get_env_run_index()
    if env_run_index is not None:
        return env_run_index
    info = _lazy_export("load_task_info")(os.path.dirname(pyr_config), raise_error=True)
    return _lazy_export("run_slot_count")(info)


def get_artifact_dir() -> str:
    """Return the current run's artifact directory, creating it when needed."""
    task_dir = get_task_dir()
    base_dir = task_dir if task_dir else os.getcwd()

    run_index = get_run_index() or _get_env_run_index() or 1
    artifact_dir = os.path.join(base_dir, ARTIFACTS_DIR, f"run{run_index}")
    os.makedirs(artifact_dir, exist_ok=True)
    return artifact_dir


def artifact_dir() -> str:
    """Return the current run's artifact directory, creating it when needed."""
    return get_artifact_dir()
