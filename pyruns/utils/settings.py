"""
Workspace settings — loads / generates ``_pyruns_.yaml`` in the tasks root.

The file is auto-created on first ``pyr`` launch.  Users can edit it to
customise UI defaults (refresh intervals, column counts, workers …).
"""
import os
import yaml
from typing import Any, Dict

from pyruns._config import SETTINGS_FILENAME


_DEFAULTS: Dict[str, Any] = {
    # Server
    "ui_port": 8080,
    # Header
    "header_refresh_interval": 3,       # seconds
    # Generator
    "generator_form_columns": 2,        # 1-9
    "generator_auto_timestamp": True,   # auto-name tasks with timestamp
    # Manager
    "manager_columns": 5,               # 1-9
    "manager_max_workers": 1,
    "manager_execution_mode": "thread", # thread | process
    "manager_poll_interval": 2,         # seconds
    "manager_page_size": 50,            # cards per page (0 = show all)
    # Monitor
    "monitor_poll_interval": 1,         # seconds
    # Logging
    "log_enabled": True,                # enable/disable pyruns internal logging
    "log_level": "INFO",                # DEBUG | INFO | WARNING | ERROR | CRITICAL
    # State (persisted across sessions)
    "starred_params": [],               # list of dotted param keys
}

_TEMPLATE = """\
# ═══════════════════════════════════════════════════════════════
#  Pyruns Workspace Settings
#  Auto-generated on first launch — edit freely to customise.
#  Delete this file to reset all values to defaults.
# ═══════════════════════════════════════════════════════════════

# ── Server ──────────────────────────────────────────────────
ui_port: 8080                      # web UI port

# ── Header ──────────────────────────────────────────────────
header_refresh_interval: 3         # metrics refresh (seconds)

# ── Generator ───────────────────────────────────────────────
generator_form_columns: 2          # parameter editor columns (1-9)
generator_auto_timestamp: true     # auto-name tasks with timestamp

# ── Manager ─────────────────────────────────────────────────
manager_columns: 5                 # task card grid columns (1-9)
manager_max_workers: 1             # parallel worker count
manager_execution_mode: thread     # thread | process
manager_poll_interval: 2           # polling interval (seconds)
manager_page_size: 50              # cards per page (0 = show all)

# ── Monitor ─────────────────────────────────────────────────
monitor_poll_interval: 1           # polling interval (seconds)

# ── Logging ─────────────────────────────────────────────────
log_enabled: true                  # false to disable all pyruns internal logs
log_level: INFO                    # DEBUG | INFO | WARNING | ERROR | CRITICAL
"""

# ── Module-level cache ───────────────────────────────────────
_cached: Dict[str, Any] = {}


def _settings_path(root_dir: str) -> str:
    return os.path.join(root_dir, SETTINGS_FILENAME)


def ensure_settings_file(root_dir: str) -> str:
    """Create ``_pyruns_.yaml`` with defaults if it doesn't exist.

    Returns the file path.
    """
    path = _settings_path(root_dir)
    if not os.path.exists(path):
        os.makedirs(root_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_TEMPLATE)
    return path


def load_settings(root_dir: str) -> Dict[str, Any]:
    """Load settings from ``_pyruns_.yaml``, falling back to defaults.

    Result is cached in-process; call ``reload_settings`` to refresh.
    """
    global _cached
    path = _settings_path(root_dir)
    merged = dict(_DEFAULTS)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                merged.update(data)
        except Exception:
            pass  # fall back to defaults silently
    _cached = merged
    return merged


def get(key: str, default: Any = None) -> Any:
    """Quick accessor for a single setting (uses cache)."""
    if not _cached:
        return _DEFAULTS.get(key, default)
    return _cached.get(key, _DEFAULTS.get(key, default))


def reload_settings(root_dir: str) -> Dict[str, Any]:
    """Force reload from disk."""
    return load_settings(root_dir)


def save_setting(key: str, value: Any) -> None:
    """Persist a single setting to ``_pyruns_.yaml`` (read-modify-write)."""
    import pyruns._config as _cfg
    root = _cfg.ROOT_DIR
    path = _settings_path(root)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}
        data[key] = value
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False,
                      allow_unicode=True, sort_keys=False)
        _cached[key] = value
    except Exception:
        pass  # best-effort — don't crash UI on write failure
