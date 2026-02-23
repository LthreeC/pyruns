"""
Workspace settings — loads / generates ``_pyruns_.yaml`` in the tasks root.

The file is auto-created on first ``pyr`` launch.  Users can edit it to
customise UI defaults (refresh intervals, column counts, workers …).
"""
import os
import yaml
from typing import Any, Dict

from pyruns._config import SETTINGS_FILENAME, ROOT_DIR


_DEFAULTS: Dict[str, Any] = {
    # Server
    "ui_port": 8099,
    # Header
    "header_refresh_interval": 3,       # seconds
    # Generator
    "generator_form_columns": 2,        # 1-9
    "generator_auto_timestamp": True,   # auto-name tasks with timestamp
    # Manager
    "manager_columns": 5,               # 1-9
    "manager_max_workers": 1,
    "manager_execution_mode": "thread", # thread | process
    "ui_page_size": 50,                 # cards per page (0 = show all)
    # Monitor
    "monitor_chunk_size": 50000,        # bytes per chunk
    "monitor_scrollback": 100000,       # max lines in history
    # Logging
    "log_enabled": False,                # enable/disable pyruns internal logging
    "log_level": "INFO",                # DEBUG | INFO | WARNING | ERROR | CRITICAL
    # State (persisted across sessions)
    "pinned_params": [],               # list of dotted param keys, ordered by pin
}

_TEMPLATE = """\
# ═══════════════════════════════════════════════════════════════
#  Pyruns Workspace Settings
#  Auto-generated on first launch — edit freely to customise.
#  Delete this file to reset all values to defaults.
# ═══════════════════════════════════════════════════════════════

# ── Server ──────────────────────────────────────────────────
ui_port: 8099                      # web UI port

# ── Header ──────────────────────────────────────────────────
header_refresh_interval: 3         # metrics refresh (seconds)

# ── Generator ───────────────────────────────────────────────
generator_form_columns: 2          # parameter editor columns (1-9)
generator_auto_timestamp: true     # auto-name tasks with timestamp

# ── Manager ─────────────────────────────────────────────────
manager_columns: 5                 # task card grid columns (1-9)
manager_max_workers: 1             # parallel worker count
manager_execution_mode: thread     # thread | process
ui_page_size: 50                   # cards per page (0 = show all)

# ── Monitor ─────────────────────────────────────────────────
monitor_chunk_size: 50000            # bytes per chunk
monitor_scrollback: 100000           # max lines in history

# ── Logging ─────────────────────────────────────────────────
log_enabled: false                  # false to disable all pyruns internal logs
log_level: INFO                    # DEBUG | INFO | WARNING | ERROR | CRITICAL
"""

# ── Module-level cache ───────────────────────────────────────
_cached: Dict[str, Any] = {}


def _settings_path(root_dir: str = ROOT_DIR) -> str:
    from pyruns._config import DEFAULT_ROOT_NAME
    # If root_dir is .../_pyruns_/<script_name>, its parent is .../_pyruns_
    parent = os.path.dirname(os.path.abspath(root_dir))
    if os.path.basename(parent) == DEFAULT_ROOT_NAME:
        return os.path.join(parent, SETTINGS_FILENAME)
    return os.path.join(root_dir, SETTINGS_FILENAME)


def ensure_settings_file(root_dir: str = ROOT_DIR) -> str:
    """Create ``_pyruns_.yaml`` with defaults if it doesn't exist.

    Returns the file path.
    """
    path = _settings_path(root_dir)
    target_dir = os.path.dirname(path)
    if not os.path.exists(path):
        os.makedirs(target_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_TEMPLATE)
    return path


def load_settings(root_dir: str = ROOT_DIR) -> Dict[str, Any]:
    """Load settings from _pyruns_, falling back to defaults.

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
    """Quick accessor for a single setting (uses cache).

    If the cache is empty (e.g. called before ``load_settings``), we
    attempt to load from disk using the current ROOT_DIR so that early
    callers (like ``log_utils``) still see user-configured values.
    """
    if not _cached:
        try:
            load_settings(ROOT_DIR)
        except Exception:
            pass
    if not _cached:
        return _DEFAULTS.get(key, default)
    return _cached.get(key, _DEFAULTS.get(key, default))


def reload_settings(root_dir: str = ROOT_DIR) -> Dict[str, Any]:
    """Force reload from disk."""
    return load_settings(root_dir)


def save_setting(key: str, value: Any) -> None:
    """Persist a single setting to ``_pyruns_settings.yaml``.

    Uses text-level replacement to preserve YAML comments and formatting.
    Falls back to appending the key if it isn't found in the file.
    """
    import re
    root = ROOT_DIR
    path = _settings_path(root)
    try:
        # Serialize the value to a YAML-friendly string
        if isinstance(value, bool):
            val_str = "true" if value else "false"
        elif isinstance(value, list):
            if not value:
                val_str = "[]"
            else:
                # Block-style list items, indented under the key
                items = yaml.dump(value, default_flow_style=False,
                                  allow_unicode=True).rstrip("\n")
                val_str = "\n" + items
        elif value is None:
            val_str = "null"
        else:
            val_str = str(value)

        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()

            # Match the key line + any continuation lines that start with
            # "- " (for YAML list items).  This ensures we replace the
            # *entire* block for list-valued keys like pinned_params.
            pattern = re.compile(
                rf"^{re.escape(key)}\s*:.*(?:\n[ \t]*-[ \t]+.*)*",
                re.MULTILINE,
            )
            if pattern.search(text):
                new_text = pattern.sub(f"{key}: {val_str}", text)
            else:
                # Key not found — append
                new_text = text.rstrip("\n") + f"\n{key}: {val_str}\n"

            with open(path, "w", encoding="utf-8") as f:
                f.write(new_text)
        else:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"{key}: {val_str}\n")

        _cached[key] = value
    except Exception:
        pass  # best-effort — don't crash UI on write failure

