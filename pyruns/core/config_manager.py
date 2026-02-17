import os
import json
import yaml
from typing import Any, Union, Dict, List, Optional

from pyruns.utils import get_logger

logger = get_logger(__name__)


class ConfigNode:
    """配置节点：将字典递归转为对象，支持点号访问"""

    def __init__(self, data: Union[Dict, List, Any] = None):
        self._data_source = data  # 保留原始数据引用（可选）
        if isinstance(data, dict):
            for key, value in data.items():
                setattr(self, key, self._wrap(value))

    def _wrap(self, value: Any) -> Any:
        if isinstance(value, dict):
            return ConfigNode(value)
        elif isinstance(value, list):
            return [self._wrap(item) for item in value]
        return value

    def to_dict(self) -> Dict[str, Any]:
        result = {}
        for key, value in self.__dict__.items():
            if key.startswith("_"):
                continue
            result[key] = self._unwrap(value)
        return result

    def _unwrap(self, value: Any) -> Any:
        if isinstance(value, ConfigNode):
            return value.to_dict()
        elif isinstance(value, list):
            return [self._unwrap(item) for item in value]
        return value

    def __repr__(self):
        # 仿 argparse 打印风格
        args = [f"{k}={repr(v)}" for k, v in self.__dict__.items() if not k.startswith("_")]
        return f"ConfigNode({', '.join(args)})"


class ConfigManager:
    def __init__(self):
        self._root: Optional[ConfigNode] = None

    def read(self, file_path: str):
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Config file not found: {file_path}")
        ext = os.path.splitext(file_path)[1].lower()
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                if ext in (".yaml", ".yml"):
                    data = yaml.safe_load(f)
                elif ext == ".json":
                    data = json.load(f)
                else:
                    raise ValueError(f"Unsupported format: {ext}")
            # 处理根节点是列表的情况
            if isinstance(data, list):
                self._root = [ConfigNode(item) if isinstance(item, dict) else item for item in data]
            else:
                self._root = ConfigNode(data or {})
            logger.info("Config loaded: %s", file_path)
        except Exception as e:
            logger.error("Failed to parse config %s: %s", file_path, e)
            raise RuntimeError(f"Failed to parse config: {e}")

    def load(self) -> Union[ConfigNode, List, None]:
        if self._root is None:
            raise RuntimeError("Config not loaded. Call read() first.")
        return self._root

