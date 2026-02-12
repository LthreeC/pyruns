import os

# Base directory for storing run data
# This can be overridden by PYRUNS_ROOT env var
ROOT_DIR = os.getenv("PYRUNS_ROOT", os.path.join(os.getcwd(), "_pyruns_"))
print(ROOT_DIR, "[DEBUG] ROOT_DIR")

# Ensure root dir exists
if not os.path.exists(ROOT_DIR):
    os.makedirs(ROOT_DIR, exist_ok=True)

# File names
INFO_FILENAME = "task_info.json"
CONFIG_FILENAME = "config.yaml"
LOG_FILENAME = "run.log"
RERUN_LOG_DIR = "rerun_logs"
TRASH_DIR = ".trash"

# UI Constants
THEME_COLOR = "indigo"
HEADER_GRADIENT = "bg-gradient-to-r from-[#0f172a] to-[#312e81]"
BG_COLOR = "bg-slate-50"
