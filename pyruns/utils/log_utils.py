import logging
import re
import sys
import threading
from typing import Dict, Optional

_LOG_CONFIG = {
    "console": {
        "level": "INFO",
        "format": "\033[32m%(asctime)s \033[33m[%(levelname)s] \033[34m%(name)s:%(lineno)d \033[0m%(message)s",
        "datefmt": "%Y-%m-%d %H:%M:%S"
    },
    "file": {
        "level": "DEBUG",
        "format": "%(asctime)s [%(levelname)s %(name)s:%(funcName)s:%(lineno)d] %(message)s"
    }
}

_LOGGER_LOCK = threading.RLock()
_LIBRARY_ROOT_LOGGER = None
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class _CloseAwareStreamHandler(logging.StreamHandler):
    """Ignore writes only after an embedding application closes the stream."""

    def emit(self, record: logging.LogRecord) -> None:
        stream = self.stream
        if stream is None or bool(getattr(stream, "closed", False)):
            return
        super().emit(record)


def _console_format_for_stream(stream, configured_format: str) -> str:
    """Use ANSI styling only for an interactive terminal stream."""

    try:
        interactive = bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        interactive = False
    return configured_format if interactive else _ANSI_ESCAPE_RE.sub("", configured_format)


def get_library_root():
    return __name__.split(".")[0]


def configure_project_root_logger(
        log_config: Optional[Dict] = None,
        *,
        force: bool = False,
):
    global _LIBRARY_ROOT_LOGGER
    log_config = log_config or _LOG_CONFIG

    with _LOGGER_LOCK:
        if _LIBRARY_ROOT_LOGGER and not force:
            return

        # Register the root logger before loading settings. Importing settings
        # reaches modules that call get_logger(), so late registration would
        # let that re-entrant call install a second console handler.
        _LIBRARY_ROOT_LOGGER = logging.getLogger(get_library_root())
        _LIBRARY_ROOT_LOGGER.propagate = False

        # Read logging settings from workspace config
        try:
            from pyruns.utils.settings import get as _get_setting
            log_enabled = _get_setting("log_enabled", True)
            log_level = _get_setting("log_level", "INFO").upper()
        except Exception:
            log_enabled = True
            log_level = "INFO"

        if force:
            for handler in list(_LIBRARY_ROOT_LOGGER.handlers):
                if bool(getattr(handler, "_pyruns_console_handler", False)):
                    _LIBRARY_ROOT_LOGGER.removeHandler(handler)
                    handler.close()

        if not log_enabled:
            # Disable all output — log calls short-circuit after int compare
            _LIBRARY_ROOT_LOGGER.setLevel(logging.CRITICAL + 1)
            return


        # Keep stdout reserved for command results and machine-readable JSON.
        console_handler = _CloseAwareStreamHandler(sys.stderr)
        console_handler._pyruns_console_handler = True
        console_handler.setFormatter(logging.Formatter(
            _console_format_for_stream(sys.stderr, log_config["console"]["format"]),
            datefmt=log_config["console"].get("datefmt"),
        ))
        console_handler.setLevel(log_level)

        _LIBRARY_ROOT_LOGGER.addHandler(console_handler)
        _LIBRARY_ROOT_LOGGER.setLevel("DEBUG")


def attach_file_handler(log_path: str, log_config: Optional[Dict] = None) -> None:
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
