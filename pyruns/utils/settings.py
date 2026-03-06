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
    DEFAULT_MONITOR_SCROLLBACK,
    DEFAULT_MONITOR_TERMINAL_GUTTER_PX,
)


SETTINGS_DEFAULTS: Dict[str, Any] = {
    # Server
    "ui_port": DEFAULT_UI_PORT,
    # Header
    "header_refresh_interval": DEFAULT_HEADER_REFRESH_INTERVAL,
    # Generator
    "generator_form_columns": DEFAULT_GENERATOR_FORM_COLUMNS,
    "generator_auto_timestamp": DEFAULT_GENERATOR_AUTO_TIMESTAMP,
    "generator_mode": DEFAULT_GENERATOR_MODE,  # form | yaml | args
    # Manager
    "manager_columns": DEFAULT_MANAGER_COLUMNS,
    "manager_max_workers": DEFAULT_MANAGER_MAX_WORKERS,
    "manager_execution_mode": DEFAULT_MANAGER_EXECUTION_MODE,
    "ui_page_size": DEFAULT_UI_PAGE_SIZE,
    # Monitor
    "monitor_chunk_size": DEFAULT_MONITOR_CHUNK_SIZE,
    "monitor_scrollback": DEFAULT_MONITOR_SCROLLBACK,
    "monitor_terminal_gutter_px": DEFAULT_MONITOR_TERMINAL_GUTTER_PX,
    # Logging
    "log_enabled": False,
    "log_level": "INFO",
    # Persisted UI state
    "pinned_params": [],
}


SETTINGS_TEMPLATE = f"""\
# Pyruns Workspace Settings
# Auto-generated on first launch. Edit freely.
# Delete this file to reset all values to defaults.

# Server
ui_port: {SETTINGS_DEFAULTS.get("ui_port")}

# Header
header_refresh_interval: {SETTINGS_DEFAULTS.get("header_refresh_interval")}

# Generator
generator_form_columns: {SETTINGS_DEFAULTS.get("generator_form_columns")}          # parameter editor columns (1-9)
generator_auto_timestamp: {SETTINGS_DEFAULTS.get("generator_auto_timestamp")}
generator_mode: {SETTINGS_DEFAULTS.get("generator_mode")}               # form | yaml | args

# Manager
manager_columns: {SETTINGS_DEFAULTS.get("manager_columns")}
manager_max_workers: {SETTINGS_DEFAULTS.get("manager_max_workers")}
manager_execution_mode: {SETTINGS_DEFAULTS.get("manager_execution_mode")}     # thread | process
ui_page_size: {SETTINGS_DEFAULTS.get("ui_page_size")}                   # cards per page (0 = show all)

# Monitor
monitor_chunk_size: {SETTINGS_DEFAULTS.get("monitor_chunk_size")}            # bytes per chunk
monitor_scrollback: {SETTINGS_DEFAULTS.get("monitor_scrollback")}           # max lines in history
monitor_terminal_gutter_px: {SETTINGS_DEFAULTS.get("monitor_terminal_gutter_px")}   # right-side text gutter (px)

# Logging
log_enabled: {SETTINGS_DEFAULTS.get("log_enabled")}
log_level: {SETTINGS_DEFAULTS.get("log_level")}                    # DEBUG | INFO | WARNING | ERROR | CRITICAL
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
    return str(value)


def save_setting_for_root(root_dir: str, key: str, value: Any) -> None:
    """Persist one key while preserving most comments/formatting."""
    path = _settings_path(root_dir)

    try:
        val_text = _yaml_scalar_to_text(value)

        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()

            pattern = re.compile(
                rf"^{re.escape(key)}\s*:.*(?:\n[ \t]*-[ \t]+.*)*",
                re.MULTILINE,
            )
            if pattern.search(text):
                new_text = pattern.sub(f"{key}: {val_text}", text)
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
