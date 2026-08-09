"""
Cross-platform process utilities — check if a PID is alive, kill a process.
"""
import math
import os
import subprocess
import time
from typing import Any

from pyruns.utils import get_logger

logger = get_logger(__name__)
_POSIX_KILL_GRACE_SEC = 1.5
_POSIX_KILL_POLL_SEC = 0.05
_PROCESS_EXIT_TIMEOUT_SEC = 5.0
_PROCESS_CREATE_TIME_TOLERANCE_SEC = 0.01


def hidden_subprocess_kwargs() -> dict[str, int]:
    """Return flags that prevent console windows for background child processes."""

    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


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


def get_process_create_time(pid: Any) -> float | None:
    """Return the OS process creation timestamp for *pid*, if it is still addressable."""

    try:
        pid_value = int(pid)
    except (TypeError, ValueError):
        return None
    if pid_value <= 0 or _psutil is None:
        return None
    try:
        value = float(_psutil.Process(pid_value).create_time())
    except Exception:
        return None
    return value if math.isfinite(value) and value > 0 else None


def process_identity_matches(pid: Any, expected_create_time: Any) -> bool:
    """Return whether *pid* still names the process with the recorded creation time."""

    try:
        expected = float(expected_create_time)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(expected) or expected <= 0:
        return False
    actual = get_process_create_time(pid)
    return bool(
        actual is not None
        and abs(actual - expected) <= _PROCESS_CREATE_TIME_TOLERANCE_SEC
    )


def _process_tree_identities(pid: int) -> list[tuple[int, float | None]]:
    """Snapshot a process tree as PID/create-time pairs before signalling it."""

    identities: list[tuple[int, float | None]] = []
    if _psutil is not None:
        try:
            root = _psutil.Process(pid)
            processes = [root, *root.children(recursive=True)]
            for process in processes:
                try:
                    process_pid = int(process.pid)
                except Exception:
                    continue
                try:
                    created_at = float(process.create_time())
                except Exception:
                    created_at = None
                identity = (process_pid, created_at)
                if identity not in identities:
                    identities.append(identity)
        except Exception:
            pass
    if not any(identity_pid == pid for identity_pid, _created in identities):
        identities.insert(0, (pid, get_process_create_time(pid)))
    return identities


def _process_identity_is_alive(identity: tuple[int, float | None]) -> bool:
    pid, created_at = identity
    if created_at is not None:
        return process_identity_matches(pid, created_at)
    return is_pid_running(pid)


def _wait_for_process_tree_exit(
    identities: list[tuple[int, float | None]],
    *,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if not any(_process_identity_is_alive(identity) for identity in identities):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POSIX_KILL_POLL_SEC)


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


def kill_process(
    pid: int,
    expected_create_time: float | None = None,
    *,
    timeout: float = _PROCESS_EXIT_TIMEOUT_SEC,
) -> bool:
    """Terminate and verify a process tree, optionally guarding against PID reuse."""

    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if expected_create_time is not None and not process_identity_matches(
            pid,
            expected_create_time,
        ):
            logger.warning(
                "Refusing to kill PID %s because its process identity no longer matches",
                pid,
            )
            return False

        identities = _process_tree_identities(pid)
        if expected_create_time is not None and not process_identity_matches(
            pid,
            expected_create_time,
        ):
            logger.warning(
                "Refusing to kill PID %s because its process identity changed before signalling",
                pid,
            )
            return False
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=5,
                **hidden_subprocess_kwargs(),
            )
            terminated = _wait_for_process_tree_exit(identities, timeout=timeout)
            if not terminated:
                logger.warning(
                    "Process tree rooted at PID %s survived taskkill (return code %s)",
                    pid,
                    getattr(result, "returncode", "unknown"),
                )
            return terminated
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
                return not any(_process_identity_is_alive(identity) for identity in identities)
            except OSError:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    return not any(_process_identity_is_alive(identity) for identity in identities)

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
            return _wait_for_process_tree_exit(
                identities,
                timeout=min(max(0.0, float(timeout)), _POSIX_KILL_GRACE_SEC),
            )
    except Exception as exc:
        logger.warning(f"Failed to kill PID {pid}: {exc}")
        return False

