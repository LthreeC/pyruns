"""
Cross-platform process utilities — check if a PID is alive, kill a process.
"""
import os
import time
from typing import Any

from pyruns.utils import get_logger

logger = get_logger(__name__)
_POSIX_KILL_GRACE_SEC = 1.5
_POSIX_KILL_POLL_SEC = 0.05

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


def _posix_process_group_exists(killpg: Any, pgid: int) -> bool:
    try:
        killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
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
            killpg = getattr(os, "killpg", None)
            sent_group_signal = False
            try:
                if killpg is not None:
                    killpg(pid, signal.SIGTERM)
                    sent_group_signal = True
                else:
                    os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            except OSError:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    return

            deadline = time.monotonic() + _POSIX_KILL_GRACE_SEC
            group_alive_after_term = sent_group_signal
            while time.monotonic() < deadline:
                if sent_group_signal and killpg is not None:
                    if not _posix_process_group_exists(killpg, pid):
                        group_alive_after_term = False
                        break
                elif not is_pid_running(pid):
                    break
                time.sleep(_POSIX_KILL_POLL_SEC)

            kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
            try:
                if sent_group_signal and killpg is not None and group_alive_after_term:
                    killpg(pid, kill_signal)
                elif is_pid_running(pid):
                    os.kill(pid, kill_signal)
            except ProcessLookupError:
                pass
    except Exception as exc:
        logger.warning(f"Failed to kill PID {pid}: {exc}")

