"""
Internal configuration constants for Pyruns.

All environment variable names, directory/file naming conventions, system
constraints, and generator syntax tokens are defined here.  Changing a
constant in this file propagates everywhere automatically.
"""
import os

# ═══════════════════════════════════════════════════════════════
#  Environment Variable Names  (统一管理，后续改名只改这里)
# ═══════════════════════════════════════════════════════════════
ENV_KEY_ROOT = "__PYRUNS_ROOT__"           # 指定 _pyruns_ 根目录
ENV_KEY_CONFIG = "__PYRUNS_CONFIG__"       # pyr 启动任务时，指向任务的 config.yaml
ENV_KEY_SCRIPT = "__PYRUNS_SCRIPT__"       # pyr 启动时，指向用户脚本路径

# ═══════════════════════════════════════════════════════════════
#  Directory / File Names
# ═══════════════════════════════════════════════════════════════
DEFAULT_ROOT_NAME = "_pyruns_"     # 默认任务存储目录名

ROOT_DIR = os.getenv(ENV_KEY_ROOT, os.path.join(os.getcwd(), DEFAULT_ROOT_NAME))


def ensure_root_dir(root: str = None) -> None:
    """Create root (default ``ROOT_DIR``) on disk if it doesn't already exist.

    This is intentionally **not** called at import time so that merely
    importing ``pyruns`` (e.g. during ``python -m build``) does not
    create a ``_pyruns_`` directory as a side-effect.
    """
    target = root or ROOT_DIR
    if not os.path.exists(target):
        os.makedirs(target, exist_ok=True)


TASKS_DIR = "tasks"

# Task / Script metadata filenames
TASK_INFO_FILENAME = "task_info.json"
SCRIPT_INFO_FILENAME = "script_info.json"

CONFIG_FILENAME = "config.yaml"
CONFIG_DEFAULT_FILENAME = "config_default.yaml"
RUN_LOGS_DIR = "run_logs"           # unified log directory: run1.log, run2.log, …
ERROR_LOG_FILENAME = "error.log"   # Global error log for failed runs
TRASH_DIR = ".trash"
RECORDS_KEY = "records"             # Use "records" array key for stats
TRACKS_KEY = "tracks"              # Use "tracks" array key for sequence tracking
SETTINGS_FILENAME = "_pyruns_settings.yaml"  # workspace-level UI settings

# Workspace settings defaults (consumed by utils/settings.py and UI state)
SETTINGS_DEFAULTS = {
    # Server
    "ui_port": 8099,
    # Header
    "header_refresh_interval": 3,
    # Generator
    "generator_form_columns": 2,
    "generator_auto_timestamp": True,
    # Manager
    "manager_columns": 5,
    "manager_max_workers": 1,
    "manager_execution_mode": "thread",
    "ui_page_size": 50,
    # Monitor
    "monitor_chunk_size": 50000,
    "monitor_scrollback": 100000,
    # Logging
    "log_enabled": False,
    "log_level": "INFO",
    # Persisted UI state
    "pinned_params": [],
}

# ═══════════════════════════════════════════════════════════════
#  System Constraints & Constants
# ═══════════════════════════════════════════════════════════════
EXECUTION_MODES = ["thread", "process"]
CSV_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# ═══════════════════════════════════════════════════════════════
#  Generator Syntax Constants
# ═══════════════════════════════════════════════════════════════
BATCH_SEPARATOR = "|"
BATCH_ESCAPE = "\\" + BATCH_SEPARATOR
