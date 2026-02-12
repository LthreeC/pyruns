import os
from .core.config_manager import ConfigManager
from ._config import ROOT_DIR


_global_config_manager_ = ConfigManager()


def read(file_path: str = None):
    """
    读取配置文件。

    优先级:
    1. 环境变量 PYRUNS_CONFIG (pyr 启动任务时自动设置)
    2. 显式传入的 file_path
    3. 默认 ROOT_DIR/config_default.yaml (直接 python 运行时)
    """
    # pyr 模式: executor 启动子进程时会设置 PYRUNS_CONFIG 指向任务的 config.yaml
    pyr_config = os.environ.get("PYRUNS_CONFIG")
    if pyr_config:
        return _global_config_manager_.read(pyr_config)

    # 直接 python 运行: 使用指定路径或默认路径
    if not file_path:
        file_path = os.path.join(ROOT_DIR, "config_default.yaml")
    return _global_config_manager_.read(file_path)


def load():
    return _global_config_manager_.load()


def run_at(args: any):
    pass

# __all__ = [
    # "g_config"
# ]
