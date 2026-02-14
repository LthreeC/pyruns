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
LOG_FILENAME = "run.log"
RERUN_LOG_DIR = "rerun_logs"
TRASH_DIR = ".trash"
MONITOR_KEY = "monitor"            # task_info.json 中存放监控数据的字段名
SETTINGS_FILENAME = "_pyruns_.yaml"  # workspace-level UI settings

# ═══════════════════════════════════════════════════════════════
#  UI Theme Constants
# ═══════════════════════════════════════════════════════════════
THEME_COLOR = "indigo"
HEADER_GRADIENT = "bg-gradient-to-r from-[#0f172a] to-[#312e81]"
BG_COLOR = "bg-slate-50"
