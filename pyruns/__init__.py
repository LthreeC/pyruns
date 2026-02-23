"""Pyruns — lightweight Python experiment management."""
import os
import json
import time
from typing import Any, Dict, Optional

from .core.config_manager import ConfigManager
from .utils.info_io import load_task_info, save_task_info
from ._config import ROOT_DIR, ENV_CONFIG, CONFIG_DEFAULT_FILENAME, MONITOR_KEY

import sys
from importlib.metadata import version, PackageNotFoundError
try:
    __version__ = version("pyruns")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"


_global_config_manager_ = ConfigManager()

def _get_default_config_path() -> str:
    from ._config import DEFAULT_ROOT_NAME
    if os.environ.get(ENV_ROOT):
        return os.path.join(ROOT_DIR, CONFIG_DEFAULT_FILENAME)
        
    script_path = sys.argv[0] if sys.argv else ""
    if script_path and os.path.isfile(script_path):
        script_base = os.path.splitext(os.path.basename(script_path))[0]
        return os.path.join(os.getcwd(), DEFAULT_ROOT_NAME, script_base, CONFIG_DEFAULT_FILENAME)
    
    return os.path.join(ROOT_DIR, CONFIG_DEFAULT_FILENAME)


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
        file_path = _get_default_config_path()
        
    if not os.path.exists(file_path) and not os.environ.get(ENV_CONFIG):
        from ._config import DEFAULT_ROOT_NAME
        script_name = os.path.basename(sys.argv[0]) if sys.argv else "script.py"
        script_base = os.path.splitext(script_name)[0]
        print(f"\n\033[93m[pyruns] Config not found: {file_path}\033[0m\n"
              f"You can either:\n"
              f"  1. Manually create the config file at {DEFAULT_ROOT_NAME}/{script_base}/{CONFIG_DEFAULT_FILENAME}\n"
              f"  2. Or use CLI to import one: `pyr {script_name} your_config.yaml`\n")
              
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
            default_path = _get_default_config_path()
            if os.path.exists(default_path):
                _global_config_manager_.read(default_path)
            else:
                from ._config import DEFAULT_ROOT_NAME
                script_name = os.path.basename(sys.argv[0]) if sys.argv else "script.py"
                script_base = os.path.splitext(script_name)[0]
                print(f"\n\033[93m[pyruns] Config not found: {default_path}\033[0m\n"
                      f"You can either:\n"
                      f"  1. Manually create the config file at {DEFAULT_ROOT_NAME}/{script_base}/{CONFIG_DEFAULT_FILENAME}\n"
                      f"  2. Or use CLI to import one: `pyr {script_name} your_config.yaml`\n")

    return _global_config_manager_.load()

def ensure_config_default(root_dir: str = None):
    """Create ``config_default.yaml`` with defaults if it doesn't exist.

    Returns the file path.
    """
    if root_dir is None:
        root_dir = ROOT_DIR
    path = os.path.join(root_dir, CONFIG_DEFAULT_FILENAME)
    if not os.path.exists(path):
        os.makedirs(root_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# task config here")
    return path


# ═══════════════════════════════════════════════════════════════
#  Monitoring API
# ═══════════════════════════════════════════════════════════════

def add_monitor(data: Optional[Dict[str, Any]] = None, **kwargs) -> None:
    """向当前任务的 task_info.json 追加或更新监控数据。

    在 pyr 启动的任务脚本中调用::

        import pyruns
        pyruns.add_monitor(epoch=10, loss=0.234, acc=95.2)
        pyruns.add_monitor({"metric_a": 1.0}, metric_b=2.0)

    **Behavior**:
    - Data is aggregated per **Run**. Multiple calls within the same run 
      will merge into the same dictionary row.
    - Requires ``pyr`` execution (checks ``ENV_CONFIG``).

    :param data: Optional dictionary of metrics.
    :param kwargs: Keyword arguments for metrics.
    :raises TypeError: if data is not a dict.
    """
    if data is not None and not isinstance(data, dict):
        raise TypeError("add_monitor expects a dict or keyword arguments")

    pyr_config = os.environ.get(ENV_CONFIG)
    if not pyr_config:
        return  # usage outside pyr -> ignore

    task_dir = os.path.dirname(pyr_config)


    # Build update payload
    update_data: Dict[str, Any] = {}
    if data:
        update_data.update(data)
    update_data.update(kwargs)

    if not update_data:
        return

    # Read → update/append → write with simple retry
    for _attempt in range(5):
        try:
            info = load_task_info(task_dir, raise_error=True)
            
            # Determine which run we are in based on start_times length
            # len=1 -> Run 1 (index 0)
            starts = info.get("start_times", [])
            run_index = len(starts)

            # Safety fallback: if run_index is 0 (shouldn't happen under pyr execution),
            # treat it as run 1 or just append to end.
            if run_index < 1:
                run_index = 1

            if MONITOR_KEY not in info:
                info[MONITOR_KEY] = []
            
            monitors = info[MONITOR_KEY]
            while len(monitors) < run_index:
                monitors.append({})

            # Merge data into the current run's slot
            monitors[run_index - 1].update(update_data)

            save_task_info(task_dir, info)
            return
        except (json.JSONDecodeError, IOError, OSError):
            time.sleep(0.05)
    # Give up silently after retries



