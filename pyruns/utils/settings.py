"""
Workspace settings loader/saver for Pyruns.

Settings are persisted to ``_pyruns_settings.yaml`` under the workspace root.
"""

import os
import re
from typing import Any, Dict

import yaml

from pyruns._config import (
    SETTINGS_FILENAME,
    ROOT_DIR,
    DEFAULT_ROOT_NAME,
    DEFAULT_UI_PORT,
    DEFAULT_UI_PAGE_SIZE,
    DEFAULT_HEADER_REFRESH_INTERVAL,
    DEFAULT_GENERATOR_FORM_COLUMNS,
    DEFAULT_GENERATOR_AUTO_TIMESTAMP,
    DEFAULT_GENERATOR_MODE,
    DEFAULT_MANAGER_COLUMNS,
    DEFAULT_MANAGER_MAX_WORKERS,
    DEFAULT_MANAGER_EXECUTION_MODE,
    DEFAULT_MONITOR_CHUNK_SIZE,
    DEFAULT_MONITOR_LINE_HEIGHT,
    DEFAULT_MONITOR_SCROLLBACK,
    DEFAULT_MONITOR_SIDEBAR_WIDTH_PCT,
    DEFAULT_SHELL_MODE,
)


SETTINGS_DEFAULTS: Dict[str, Any] = {
    # Server
    "ui_port": DEFAULT_UI_PORT,
    # Header
    "header_refresh_interval": DEFAULT_HEADER_REFRESH_INTERVAL,
    # Generator
    "generator_form_columns": DEFAULT_GENERATOR_FORM_COLUMNS,
    "generator_auto_timestamp": DEFAULT_GENERATOR_AUTO_TIMESTAMP,
    "generator_mode": DEFAULT_GENERATOR_MODE,  # script workspace only: form | yaml
    # Manager
    "manager_columns": DEFAULT_MANAGER_COLUMNS,
    "manager_max_workers": DEFAULT_MANAGER_MAX_WORKERS,
    "manager_execution_mode": DEFAULT_MANAGER_EXECUTION_MODE,
    "ui_page_size": DEFAULT_UI_PAGE_SIZE,
    # Monitor
    "monitor_chunk_size": DEFAULT_MONITOR_CHUNK_SIZE,
    "monitor_scrollback": DEFAULT_MONITOR_SCROLLBACK,
    "monitor_line_height": DEFAULT_MONITOR_LINE_HEIGHT,
    "monitor_sidebar_width_pct": DEFAULT_MONITOR_SIDEBAR_WIDTH_PCT,
    # Logging
    "log_enabled": False,
    "log_level": "INFO",
    # Shell
    "shell_mode": DEFAULT_SHELL_MODE,
    "shell_executable": "",
    # Runtime
    "python_executable": "",
    "conda_env": "",
    "conda_executable": "conda",
    "global_env": {},
    # GPU scheduler
    "gpu_scheduler_enabled": False,
    "gpu_scheduler_task_mode": "single",
    "gpu_scheduler_selection_mode": "auto",
    "gpu_scheduler_gpus_per_task": 1,
    "gpu_scheduler_device_ids": [],
    "gpu_scheduler_memory_used_pct": 40,
    "gpu_scheduler_min_free_memory_gb": 40,
    "gpu_scheduler_compute_used_pct": 30,
    "gpu_scheduler_stable_seconds": 15,
    "gpu_scheduler_max_wait_seconds": 172800,
    "gpu_scheduler_max_tasks_per_gpu": 1,
    "gpu_scheduler_respect_cuda_visible_devices": True,
    "gpu_scheduler_require_same_gpu_model": False,
    # Persisted UI state
    "pinned_params": [],
}


SETTINGS_TEMPLATE = f"""\
# Pyruns Workspace Settings
# Auto-generated on first launch. Edit freely.
# Delete this file to reset all values to defaults.

# Server
ui_port: {SETTINGS_DEFAULTS.get("ui_port")}                    # preferred start port; busy ports auto-increment

# Header
header_refresh_interval: {SETTINGS_DEFAULTS.get("header_refresh_interval")}

# Generator
generator_form_columns: {SETTINGS_DEFAULTS.get("generator_form_columns")}          # parameter editor columns (1-9)
generator_auto_timestamp: {SETTINGS_DEFAULTS.get("generator_auto_timestamp")}
generator_mode: {SETTINGS_DEFAULTS.get("generator_mode")}               # script workspace only: form | yaml

# Manager
manager_columns: {SETTINGS_DEFAULTS.get("manager_columns")}
manager_max_workers: {SETTINGS_DEFAULTS.get("manager_max_workers")}
manager_execution_mode: {SETTINGS_DEFAULTS.get("manager_execution_mode")}     # thread | process
ui_page_size: {SETTINGS_DEFAULTS.get("ui_page_size")}                   # cards per page (0 = show all)

# Monitor
monitor_chunk_size: {SETTINGS_DEFAULTS.get("monitor_chunk_size")}            # bytes per chunk
monitor_scrollback: {SETTINGS_DEFAULTS.get("monitor_scrollback")}           # initial tail lines and xterm scrollback rows
monitor_line_height: {SETTINGS_DEFAULTS.get("monitor_line_height")}         # terminal row height multiplier (1.0 = normal)
monitor_sidebar_width_pct: {SETTINGS_DEFAULTS.get("monitor_sidebar_width_pct")}     # monitor sidebar width (% of page)

# Logging
log_enabled: {SETTINGS_DEFAULTS.get("log_enabled")}
log_level: {SETTINGS_DEFAULTS.get("log_level")}                    # DEBUG | INFO | WARNING | ERROR | CRITICAL

# Shell
shell_mode: {SETTINGS_DEFAULTS.get("shell_mode")}                  # follow | custom
shell_executable: {SETTINGS_DEFAULTS.get("shell_executable")}

# Runtime
python_executable: {SETTINGS_DEFAULTS.get("python_executable")}             # absolute Python path; empty = pyruns server Python
conda_env: {SETTINGS_DEFAULTS.get("conda_env")}                     # conda env name; applies to Python and shell tasks
conda_executable: {SETTINGS_DEFAULTS.get("conda_executable")}           # conda executable used by conda_env
global_env: {{}}                       # workspace env overrides; task env overrides this

# GPU scheduler
gpu_scheduler_enabled: {SETTINGS_DEFAULTS.get("gpu_scheduler_enabled")}             # false = run normally; true = wait for eligible local GPUs
gpu_scheduler_task_mode: {SETTINGS_DEFAULTS.get("gpu_scheduler_task_mode")}          # single | multi
gpu_scheduler_selection_mode: {SETTINGS_DEFAULTS.get("gpu_scheduler_selection_mode")} # auto | specified
gpu_scheduler_gpus_per_task: {SETTINGS_DEFAULTS.get("gpu_scheduler_gpus_per_task")}  # used when task_mode is multi
gpu_scheduler_device_ids: []             # auto mode: pool; specified mode: exact GPU IDs
gpu_scheduler_memory_used_pct: {SETTINGS_DEFAULTS.get("gpu_scheduler_memory_used_pct")}      # eligible when memory used is below this percent
gpu_scheduler_min_free_memory_gb: {SETTINGS_DEFAULTS.get("gpu_scheduler_min_free_memory_gb")} # eligible when free memory is at least this many GiB
gpu_scheduler_compute_used_pct: {SETTINGS_DEFAULTS.get("gpu_scheduler_compute_used_pct")}     # eligible when GPU compute is below this percent
gpu_scheduler_stable_seconds: {SETTINGS_DEFAULTS.get("gpu_scheduler_stable_seconds")}         # limits must stay eligible for this long
gpu_scheduler_max_wait_seconds: {SETTINGS_DEFAULTS.get("gpu_scheduler_max_wait_seconds")}     # default 48h
gpu_scheduler_max_tasks_per_gpu: {SETTINGS_DEFAULTS.get("gpu_scheduler_max_tasks_per_gpu")}
gpu_scheduler_respect_cuda_visible_devices: {SETTINGS_DEFAULTS.get("gpu_scheduler_respect_cuda_visible_devices")}
gpu_scheduler_require_same_gpu_model: {SETTINGS_DEFAULTS.get("gpu_scheduler_require_same_gpu_model")}

"""


_cached: Dict[str, Any] = {}


def _settings_path(root_dir: str = ROOT_DIR) -> str:
    """Resolve settings path.

    If ``root_dir`` is ``.../_pyruns_/<script_name>``, settings live in
    ``.../_pyruns_/_pyruns_settings.yaml``.
    """
    parent = os.path.dirname(os.path.abspath(root_dir))
    if os.path.basename(parent) == DEFAULT_ROOT_NAME:
        return os.path.join(parent, SETTINGS_FILENAME)
    return os.path.join(root_dir, SETTINGS_FILENAME)


def ensure_settings_file(root_dir: str = ROOT_DIR) -> str:
    """Create settings file with defaults if it does not exist."""
    path = _settings_path(root_dir)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(SETTINGS_TEMPLATE)
    return path


def load_settings(root_dir: str = ROOT_DIR) -> Dict[str, Any]:
    """Load and cache settings from disk with defaults merged in."""
    global _cached
    path = _settings_path(root_dir)
    merged = dict(SETTINGS_DEFAULTS)

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                merged.update(data)
        except Exception:
            pass

    _cached = merged
    return merged


def reload_settings(root_dir: str = ROOT_DIR) -> Dict[str, Any]:
    """Force reload settings from disk."""
    return load_settings(root_dir)


def get(key: str, default: Any = None) -> Any:
    """Get one setting value from cache (lazy-loading if needed)."""
    if not _cached:
        try:
            load_settings(ROOT_DIR)
        except Exception:
            pass

    if not _cached:
        return SETTINGS_DEFAULTS.get(key, default)
    return _cached.get(key, SETTINGS_DEFAULTS.get(key, default))


def save_setting(key: str, value: Any) -> None:
    """Persist a single setting for current ROOT_DIR."""
    save_setting_for_root(ROOT_DIR, key, value)


def _yaml_scalar_to_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, list):
        if not value:
            return "[]"
        return "\n" + yaml.dump(value, default_flow_style=False, allow_unicode=True).rstrip("\n")
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "\n" + yaml.dump(value, default_flow_style=False, allow_unicode=True, sort_keys=False).rstrip("\n")
    return str(value)


def save_setting_for_root(root_dir: str, key: str, value: Any) -> None:
    """Persist one key while preserving most comments/formatting."""
    path = _settings_path(root_dir)

    try:
        val_text = _yaml_scalar_to_text(value)

        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()

            # Match the key line and simple continuation lines (YAML list/dict items).
            pattern = re.compile(
                rf"^{re.escape(key)}\s*:.*(?:\n[ \t]+(?:-[ \t]+.*|[^:\n]+:.*))*",
                re.MULTILINE,
            )

            if isinstance(value, (dict, list)) and value:
                # For non-empty lists, use full YAML reload-and-dump to avoid
                # regex substitution issues with multi-line structured values.
                try:
                    data = yaml.safe_load(text) or {}
                    if isinstance(data, dict):
                        data[key] = value
                        # Re-dump the entire file to ensure correct formatting
                        new_text = yaml.dump(
                            data, default_flow_style=False,
                            allow_unicode=True, sort_keys=False,
                        )
                    else:
                        new_text = text.rstrip("\n") + f"\n{key}: {val_text}\n"
                except Exception:
                    # Fallback to regex approach
                    if pattern.search(text):
                        new_text = pattern.sub(lambda _: f"{key}: {val_text}", text)
                    else:
                        new_text = text.rstrip("\n") + f"\n{key}: {val_text}\n"
            else:
                if pattern.search(text):
                    new_text = pattern.sub(lambda _: f"{key}: {val_text}", text)
                else:
                    new_text = text.rstrip("\n") + f"\n{key}: {val_text}\n"

            with open(path, "w", encoding="utf-8") as f:
                f.write(new_text)
        else:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"{key}: {val_text}\n")

        _cached[key] = value
    except Exception:
        # Best effort: do not break UI on settings write failure.
        pass
