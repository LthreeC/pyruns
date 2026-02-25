"""
Cross-platform process utilities — check if a PID is alive, kill a process.
"""
import os
from typing import Any

from pyruns.utils import get_logger

logger = get_logger(__name__)


def is_pid_running(pid: Any) -> bool:
    """Check whether *pid* is still alive (cross-platform)."""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False

    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def kill_process(pid: int) -> None:
    """Terminate the process tree rooted at *pid* (cross-platform)."""
    try:
        if os.name == "nt":
            import subprocess as _sp
            _sp.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=5,
            )
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
    except Exception as exc:
        logger.warning(f"Failed to kill PID {pid}: {exc}")

