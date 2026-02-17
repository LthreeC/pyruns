import logging
import sys
import threading
from typing import Dict, Optional

_LOG_CONFIG = {
    "console": {
        "level": "INFO",
        "format": f"\033[32m%(asctime)s \033[33m[%(levelname)s] \033[34m%(name)s:%(lineno)d \033[0m%(message)s",
        "datefmt": "%Y-%m-%d %H:%M:%S"
    },
    "file": {
        "level": "DEBUG",
        "format": f"%(asctime)s [%(levelname)s %(name)s:%(funcName)s:%(lineno)d] %(message)s"
    }
}

_LOGGER_LOCK = threading.RLock()
_LIBRARY_ROOT_LOGGER = None


def get_library_root():
    return __name__.split(".")[0]


def configure_project_root_logger(
        log_config: Optional[Dict] = None
):
    global _LIBRARY_ROOT_LOGGER
    log_config = log_config or _LOG_CONFIG

    with _LOGGER_LOCK:
        if _LIBRARY_ROOT_LOGGER:
            return

        # Read logging settings from workspace config
        try:
            from pyruns.utils.settings import get as _get_setting
            log_enabled = _get_setting("log_enabled", True)
            log_level = _get_setting("log_level", "INFO").upper()
        except Exception:
            log_enabled = True
            log_level = "INFO"

        _LIBRARY_ROOT_LOGGER = logging.getLogger(get_library_root())
        _LIBRARY_ROOT_LOGGER.propagate = False

        if not log_enabled:
            # Disable all output — log calls short-circuit after int compare
            _LIBRARY_ROOT_LOGGER.setLevel(logging.CRITICAL + 1)
            return


        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter(
            log_config["console"]["format"],
            datefmt=log_config["console"].get("datefmt"),
        ))
        console_handler.setLevel(log_level)

        _LIBRARY_ROOT_LOGGER.addHandler(console_handler)
        _LIBRARY_ROOT_LOGGER.setLevel("DEBUG")


def attach_file_handler(log_path: str, log_config: str = None) -> None:
    global _LIBRARY_ROOT_LOGGER
    log_config = log_config or _LOG_CONFIG

    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(log_config["file"]["format"], datefmt=log_config["console"].get("datefmt", None)))
    file_handler.setLevel(log_config["file"]["level"])

    _LIBRARY_ROOT_LOGGER.addHandler(file_handler)


def get_logger(name: str = None):
    if name == "__main__":
        name = get_library_root() + ".__main__"
    configure_project_root_logger()
    return logging.getLogger(name)
