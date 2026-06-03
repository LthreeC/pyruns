"""
Cross-platform process utilities — check if a PID is alive, kill a process.
"""
import os
from typing import Any

from pyruns.utils import get_logger

logger = get_logger(__name__)

# Import psutil at module level so tests can mock it via
# @patch("pyruns.utils.process_utils.psutil")
try:
    import psutil as _psutil
except ImportError:
    _psutil = None  # type: ignore[assignment]


def is_pid_running(pid: Any) -> bool:
    """Check whether *pid* is still alive (cross-platform).

    Uses psutil when available for broad cross-platform detection.
    Falls back to OS-level checks otherwise.
    """
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False

    if _psutil is not None:
        try:
            return _psutil.pid_exists(pid)
        except Exception:
            pass

    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                exit_code = ctypes.c_ulong()
                STILL_ACTIVE = 259
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    is_active = (exit_code.value == STILL_ACTIVE)
                    kernel32.CloseHandle(handle)
                    return is_active
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

