import os

# ═══════════════════════════════════════════════════════════════
#  Environment Variable Names  (统一管理，后续改名只改这里)
# ═══════════════════════════════════════════════════════════════
ENV_ROOT = "PYRUNS_ROOT"           # 指定 _pyruns_ 根目录
ENV_CONFIG = "PYRUNS_CONFIG"       # pyr 启动任务时，指向任务的 config.yaml
ENV_SCRIPT = "PYRUNS_SCRIPT"       # pyr 启动时，指向用户脚本路径

# ═══════════════════════════════════════════════════════════════
#  Directory / File Names
# ═══════════════════════════════════════════════════════════════
DEFAULT_ROOT_NAME = "_pyruns_"     # 默认任务存储目录名

ROOT_DIR = os.getenv(ENV_ROOT, os.path.join(os.getcwd(), DEFAULT_ROOT_NAME))

# Ensure root dir exists
if not os.path.exists(ROOT_DIR):
    os.makedirs(ROOT_DIR, exist_ok=True)

# Task 内部文件/目录名
INFO_FILENAME = "task_info.json"
CONFIG_FILENAME = "config.yaml"
CONFIG_DEFAULT_FILENAME = "config_default.yaml"
RUN_LOG_DIR = "run_logs"           # unified log directory: run1.log, run2.log, …
ERROR_LOG_FILENAME = "error.log"   # Global error log for failed runs
TRASH_DIR = ".trash"
MONITOR_KEY = "monitors"           # Use "monitors" array key for stats
SETTINGS_FILENAME = "_pyruns_settings.yaml"  # workspace-level UI settings