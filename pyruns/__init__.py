"""Pyruns — lightweight Python experiment management."""
import os
import json
import time
import datetime
from typing import Any, Dict, Optional

from .core.config_manager import ConfigManager
from ._config import ROOT_DIR, ENV_CONFIG, CONFIG_DEFAULT_FILENAME, INFO_FILENAME, MONITOR_KEY

from importlib.metadata import version, PackageNotFoundError
try:
    __version__ = version("pyruns")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"


_global_config_manager_ = ConfigManager()


def read(file_path: str = None):
    """
    读取配置文件。

    优先级:
    1. 环境变量 ENV_CONFIG (pyr 启动任务时自动设置)
    2. 显式传入的 file_path
    3. 默认 ROOT_DIR/config_default.yaml (直接 python 运行时)
    """
    # pyr 模式: executor 启动子进程时会设置 ENV_CONFIG 指向任务的 config.yaml
    pyr_config = os.environ.get(ENV_CONFIG)
    if pyr_config:
        return _global_config_manager_.read(pyr_config)

    # 直接 python 运行: 使用指定路径或默认路径
    if not file_path:
        file_path = os.path.join(ROOT_DIR, CONFIG_DEFAULT_FILENAME)
    return _global_config_manager_.read(file_path)


def load():
    """Return the loaded config (auto-reads under ``pyr`` if not yet loaded)."""
    if _global_config_manager_._root is None:
        # Auto-read when running under pyr (ENV_CONFIG is set by executor)
        pyr_config = os.environ.get(ENV_CONFIG)
        if pyr_config:
            _global_config_manager_.read(pyr_config)
        else:
            # Fallback: try default config path
            default_path = os.path.join(ROOT_DIR, CONFIG_DEFAULT_FILENAME)
            if os.path.exists(default_path):
                _global_config_manager_.read(default_path)
    return _global_config_manager_.load()




# ═══════════════════════════════════════════════════════════════
#  Monitoring API
# ═══════════════════════════════════════════════════════════════

def add_monitor(data: Optional[Dict[str, Any]] = None, **kwargs) -> None:
    """向当前任务的 task_info.json 追加监控数据。

    在 pyr 启动的任务脚本中调用::

        import pyruns
        pyruns.add_monitor(epoch=10, loss=0.234, acc=95.2)
        pyruns.add_monitor({"metric_a": 1.0, "metric_b": 2.0})

    每次调用会自动附加时间戳 ``_ts``。数据追加到
    ``task_info.json`` 的 ``"monitor"`` 列表字段中。

    如果不在 pyr 管理的任务中运行 (无 ENV_CONFIG 环境变量)，
    调用会被静默忽略。
    """
    pyr_config = os.environ.get(ENV_CONFIG)
    if not pyr_config:
        return  # 不在 pyr 管理下，静默跳过

    task_dir = os.path.dirname(pyr_config)
    info_path = os.path.join(task_dir, INFO_FILENAME)

    # Build entry
    entry: Dict[str, Any] = {}
    if data and isinstance(data, dict):
        entry.update(data)
    entry.update(kwargs)
    entry["_ts"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Read → append → write with simple retry
    for _attempt in range(5):
        try:
            info: Dict[str, Any] = {}
            if os.path.exists(info_path):
                with open(info_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
            if MONITOR_KEY not in info:
                info[MONITOR_KEY] = []
            info[MONITOR_KEY].append(entry)
            with open(info_path, "w", encoding="utf-8") as f:
                json.dump(info, f, indent=4, ensure_ascii=False)
            return
        except (json.JSONDecodeError, IOError, OSError):
            time.sleep(0.05)
    # 如果所有重试都失败，静默忽略（不影响用户脚本运行）


def get_monitor_data(task_dir: str) -> list:
    """读取指定任务目录的监控数据列表。供 UI 使用。"""
    info_path = os.path.join(task_dir, INFO_FILENAME)
    try:
        with open(info_path, "r", encoding="utf-8") as f:
            info = json.load(f)
        return info.get(MONITOR_KEY, [])
    except Exception:
        return []
