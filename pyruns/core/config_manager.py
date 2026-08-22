"""OmegaConf-backed configuration loading for Pyruns scripts."""

from __future__ import annotations

import json
import os
from typing import Optional

from omegaconf import DictConfig, ListConfig, OmegaConf

from pyruns._config import MAX_CONFIG_FILE_BYTES
from pyruns.utils import get_logger
from pyruns.utils.config_utils import load_config_text

logger = get_logger(__name__)

ConfigValue = DictConfig | ListConfig


def _read_config_bytes(file_path: str) -> bytes:
    with open(file_path, "rb") as handle:
        raw = handle.read(MAX_CONFIG_FILE_BYTES + 1)
    if len(raw) > MAX_CONFIG_FILE_BYTES:
        raise ValueError(
            f"Config file is too large (max {MAX_CONFIG_FILE_BYTES} bytes): {file_path}"
        )
    return raw


class ConfigManager:
    """Load and retain one OmegaConf configuration tree."""

    def __init__(self):
        self._root: Optional[ConfigValue] = None

    def read(self, file_path: str) -> None:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Config file not found: {file_path}")

        ext = os.path.splitext(file_path)[1].lower()
        try:
            raw = _read_config_bytes(file_path)
            text = raw.decode("utf-8-sig")
            if ext in (".yaml", ".yml"):
                config = load_config_text(text)
            elif ext == ".json":
                config = OmegaConf.create(json.loads(text))
                if not isinstance(config, (DictConfig, ListConfig)):
                    raise ValueError("Configuration root must be a mapping or list")
            else:
                raise ValueError(f"Unsupported format: {ext}")

            self._root = config
            logger.info("Config loaded: %s", file_path)
        except Exception as exc:
            logger.error("Failed to parse config %s: %s", file_path, exc)
            raise RuntimeError(f"Failed to parse config: {exc}") from exc

    def load(self) -> ConfigValue:
        if self._root is None:
            raise RuntimeError("Config not loaded. Call read() first.")
        return self._root
