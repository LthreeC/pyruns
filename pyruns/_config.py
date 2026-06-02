"""
Internal configuration constants for Pyruns.

Changing a constant here should propagate through the core runtime, web API,
CLI helpers, and UI layers.
"""

from __future__ import annotations

import os

# Environment variables
ENV_KEY_ROOT = "__PYRUNS_ROOT__"
ENV_KEY_CONFIG = "__PYRUNS_CONFIG__"
ENV_KEY_SHELL = "PYRUNS_SHELL_EXECUTABLE"
ENV_KEY_RUN_INDEX = "PYRUNS_RUN_INDEX"
ENV_KEY_PYTHON_EXECUTABLE = "PYRUNS_PYTHON_EXECUTABLE"
ENV_KEY_CONDA_ENV = "PYRUNS_CONDA_ENV"
ENV_KEY_CONDA_EXE = "PYRUNS_CONDA_EXE"
ENV_KEY_CLI_TERMINAL_RUNTIME = "PYRUNS_CLI_TERMINAL_RUNTIME"

# Directory / file names
DEFAULT_ROOT_NAME = "_pyruns_"
SHELL_WORKSPACE_NAME = "_shell_"

ROOT_DIR = os.getenv(ENV_KEY_ROOT, os.path.join(os.getcwd(), DEFAULT_ROOT_NAME))


def ensure_root_dir(root: str | None = None) -> None:
    """Create the root directory on disk if it does not already exist."""

    target = root or ROOT_DIR
    if not os.path.exists(target):
        os.makedirs(target, exist_ok=True)


TASKS_DIR = "tasks"

TASK_INFO_FILENAME = "task_info.json"
SCRIPT_INFO_FILENAME = "script_info.json"

CONFIG_FILENAME = "config.yaml"
CONFIG_DEFAULT_FILENAME = "config_default.yaml"
SHELL_CONFIG_FILENAME = "config.sh"
POWERSHELL_CONFIG_FILENAME = "config.ps1"
CMD_CONFIG_FILENAME = "config.cmd"
FISH_CONFIG_FILENAME = "config.fish"
RUN_LOGS_DIR = "run_logs"
ARTIFACTS_DIR = "artifacts"
ERROR_LOG_FILENAME = "error.log"
TRASH_DIR = ".trash"
RECORDS_KEY = "records"
TRACKS_KEY = "tracks"
SETTINGS_FILENAME = "_pyruns_settings.yaml"

# Workspace / task kinds
WORKSPACE_KIND_SCRIPT = "script"
WORKSPACE_KIND_SHELL = "shell"
TASK_KIND_PYTHON = "python"
TASK_KIND_CONFIG = TASK_KIND_PYTHON
TASK_KIND_SHELL = "shell"

WORKSPACE_KINDS = (
    WORKSPACE_KIND_SCRIPT,
    WORKSPACE_KIND_SHELL,
)

TASK_KINDS = (
    TASK_KIND_CONFIG,
    TASK_KIND_SHELL,
)

TASK_KIND_TO_CONFIG_FILENAME = {
    TASK_KIND_CONFIG: CONFIG_FILENAME,
    TASK_KIND_SHELL: SHELL_CONFIG_FILENAME,
}

SHELL_KIND_TO_CONFIG_FILENAME = {
    "powershell": POWERSHELL_CONFIG_FILENAME,
    "cmd": CMD_CONFIG_FILENAME,
    "bash": SHELL_CONFIG_FILENAME,
    "sh": SHELL_CONFIG_FILENAME,
    "zsh": SHELL_CONFIG_FILENAME,
    "fish": FISH_CONFIG_FILENAME,
}

SHELL_CONFIG_FILENAMES = (
    SHELL_CONFIG_FILENAME,
    POWERSHELL_CONFIG_FILENAME,
    CMD_CONFIG_FILENAME,
    FISH_CONFIG_FILENAME,
)

# System constraints & defaults
EXECUTION_MODES = ["thread", "process"]
CSV_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

DEFAULT_UI_PORT = 8099
DEFAULT_UI_PAGE_SIZE = 50
DEFAULT_HEADER_REFRESH_INTERVAL = 3

DEFAULT_GENERATOR_FORM_COLUMNS = 3
DEFAULT_GENERATOR_AUTO_TIMESTAMP = True
DEFAULT_GENERATOR_MODE = "form"  # script workspace: form | yaml

DEFAULT_MANAGER_COLUMNS = 5
DEFAULT_MANAGER_MAX_WORKERS = 1
DEFAULT_MANAGER_EXECUTION_MODE = "thread"

DEFAULT_MONITOR_CHUNK_SIZE = 50000
DEFAULT_MONITOR_SCROLLBACK = 100000
DEFAULT_MONITOR_SIDEBAR_WIDTH_PCT = 15

DEFAULT_SHELL_MODE = "follow"

# Generator syntax
BATCH_SEPARATOR = "|"
BATCH_ESCAPE = "\\" + BATCH_SEPARATOR
