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


def _psutil_process_is_alive(pid: int) -> bool | None:
    """Return psutil liveness, treating zombies as exited, or None on failure."""

    if _psutil is None:
        return None
    try:
        status = _psutil.Process(pid).status()
    except Exception:
        return None
    exited_statuses = {
        getattr(_psutil, "STATUS_ZOMBIE", "zombie"),
        getattr(_psutil, "STATUS_DEAD", "dead"),
    }
    return status not in exited_statuses


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

    psutil_alive = _psutil_process_is_alive(pid)
    if psutil_alive is not None:
        return psutil_alive

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
    psutil_alive = _psutil_process_is_alive(pid)
    if psutil_alive is False:
        return False
    if created_at is not None:
        return process_identity_matches(pid, created_at)
    if psutil_alive is True:
        return True
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


def _signal_posix_process_tree(
    identities: list[tuple[int, float | None]],
    signal_number: int,
) -> None:
    """Signal every snapshotted process group without touching our own group."""

    killpg = getattr(os, "killpg", None)
    try:
        current_group = int(os.getpgrp())
    except (AttributeError, OSError):
        current_group = -1

    # Signal the root group first so the parent cannot keep spawning work while
    # independently-sessioned descendants are being stopped.
    for identity in identities:
        process_pid, _created_at = identity
        if not _process_identity_is_alive(identity):
            continue
        try:
            process_group = int(os.getpgid(process_pid))
        except (AttributeError, OSError, ProcessLookupError):
            process_group = None

        group_signalled = False
        if (
            killpg is not None
            and process_group is not None
            and process_group == process_pid
            and process_group != current_group
        ):
            try:
                killpg(process_group, signal_number)
                group_signalled = True
            except (OSError, ProcessLookupError):
                pass
        if group_signalled or not _process_identity_is_alive(identity):
            continue
        try:
            os.kill(process_pid, signal_number)
        except (OSError, ProcessLookupError):
            pass


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
            grace_timeout = min(
                max(0.0, float(timeout)),
                max(0.0, _POSIX_KILL_GRACE_SEC),
            )
            _signal_posix_process_tree(identities, signal.SIGTERM)
            if _wait_for_process_tree_exit(identities, timeout=grace_timeout):
                return True

            kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
            _signal_posix_process_tree(identities, kill_signal)
            return _wait_for_process_tree_exit(
                identities,
                timeout=grace_timeout,
            )
    except Exception as exc:
        logger.warning(f"Failed to kill PID {pid}: {exc}")
        return False
