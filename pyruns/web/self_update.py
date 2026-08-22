"""Full-process Pyruns upgrade and UI restart helpers."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from packaging.version import InvalidVersion, Version


UI_PRODUCTION_RESTART_ENV = "PYRUNS_UI_PRODUCTION_RESTART"
UI_TOKEN_ENV = "PYRUNS_UI_TOKEN"
UI_UPDATE_RESULT_ENV = "PYRUNS_UI_UPDATE_RESULT"
PYPI_PROJECT_JSON_URL = "https://pypi.org/pypi/pyruns/json"
PYPI_CHECK_TIMEOUT_SECONDS = 5.0
PYPI_MAX_RESPONSE_BYTES = 1_048_576


class ActiveTasksError(RuntimeError):
    """Raised when an upgrade is requested while tasks are still active."""

    def __init__(self, active_count: int) -> None:
        self.active_count = max(1, int(active_count))
        super().__init__(
            f"Pyruns can update only while idle; {self.active_count} queued or running "
            f"task{'s' if self.active_count != 1 else ''} remain."
        )


class UpdateInProgressError(RuntimeError):
    """Raised when new work is submitted after an upgrade has started."""


class UpdateCheckError(RuntimeError):
    """Raised when Pyruns cannot establish that every managed workspace is idle."""


class LatestVersionCheckError(RuntimeError):
    """Raised when the latest published Pyruns version cannot be verified."""


class UiUpdateCoordinator:
    """Atomically gate task starts while a production UI prepares to restart."""

    def __init__(self, shutdown_callback: Callable[[], None]) -> None:
        self._shutdown_callback = shutdown_callback
        self._lock = threading.RLock()
        self._requested = False

    @property
    def requested(self) -> bool:
        with self._lock:
            return self._requested

    @property
    def state(self) -> str:
        return "restarting" if self.requested else "idle"

    @contextmanager
    def task_start_guard(self) -> Iterator[None]:
        """Reject starts once update preparation has established an idle runtime."""

        with self._lock:
            if self._requested:
                raise UpdateInProgressError(
                    "Pyruns is updating; new tasks are disabled until the UI restarts."
                )
            yield

    def prepare(self, runtime: Any) -> None:
        """Mark the UI for restart only after a synchronized idle check."""

        with self._lock:
            if self._requested:
                return
            try:
                active_count = int(runtime.active_task_count())
            except Exception as exc:
                raise UpdateCheckError(
                    "Could not verify that every managed workspace is idle."
                ) from exc
            if active_count > 0:
                raise ActiveTasksError(active_count)
            self._requested = True

    def trigger_shutdown(self) -> None:
        """Ask Uvicorn to finish after the update response has been sent."""

        self._shutdown_callback()


def check_latest_version(current_version: str) -> dict[str, Any]:
    """Read PyPI's bounded project metadata and compare PEP 440 versions."""

    request = urllib.request.Request(
        PYPI_PROJECT_JSON_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": f"pyruns/{current_version} update-check",
        },
    )
    try:
        with urllib.request.urlopen(  # nosec B310 - the target is a fixed HTTPS URL
            request,
            timeout=PYPI_CHECK_TIMEOUT_SECONDS,
        ) as response:
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > PYPI_MAX_RESPONSE_BYTES:
                raise LatestVersionCheckError("PyPI returned an unexpectedly large response.")
            raw = response.read(PYPI_MAX_RESPONSE_BYTES + 1)
    except LatestVersionCheckError:
        raise
    except (OSError, TimeoutError, ValueError, urllib.error.URLError) as exc:
        raise LatestVersionCheckError(
            "Could not check PyPI for a newer Pyruns release. Check the network and try again."
        ) from exc

    if len(raw) > PYPI_MAX_RESPONSE_BYTES:
        raise LatestVersionCheckError("PyPI returned an unexpectedly large response.")
    try:
        payload = json.loads(raw.decode("utf-8"))
        latest_version = str(payload["info"]["version"]).strip()
        current = Version(str(current_version))
        latest = Version(latest_version)
    except (KeyError, TypeError, UnicodeError, ValueError, InvalidVersion) as exc:
        raise LatestVersionCheckError("PyPI returned invalid Pyruns version metadata.") from exc
    if not latest_version:
        raise LatestVersionCheckError("PyPI returned invalid Pyruns version metadata.")
    return {
        "current_version": str(current_version),
        "latest_version": latest_version,
        "update_available": latest > current,
    }


def read_update_result() -> dict[str, Any] | None:
    """Read the bounded result passed from the one-shot updater process."""

    raw = str(os.getenv(UI_UPDATE_RESULT_ENV, "") or "")
    if not raw or len(raw) > 8_192:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        return None
    try:
        exit_code = int(payload.get("exit_code", 1))
    except (TypeError, ValueError):
        return None
    return {
        "ok": payload["ok"],
        "previous_version": str(payload.get("previous_version", "") or ""),
        "installed_version": str(payload.get("installed_version", "") or ""),
        "exit_code": exit_code,
    }


def replace_process_with_updater(*, port: int, token: str, previous_version: str) -> None:
    """Replace the current UI process so package files are no longer in use."""

    command = [
        sys.executable,
        "-m",
        "pyruns.web.self_update",
        "--port",
        str(int(port)),
        "--previous-version",
        str(previous_version),
    ]
    environment = os.environ.copy()
    environment[UI_TOKEN_ENV] = str(token)
    print("[pyruns] UI is idle; starting full Pyruns upgrade.", flush=True)
    os.execve(sys.executable, command, environment)


def _query_installed_version(fallback: str) -> str:
    command = [
        sys.executable,
        "-c",
        "from importlib.metadata import version; print(version('pyruns'))",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return fallback
    version_text = str(completed.stdout or "").strip()
    return version_text if completed.returncode == 0 and version_text else fallback


def run_pip_upgrade(previous_version: str) -> dict[str, Any]:
    """Run the requested pip operation and return a relaunch-safe result."""

    command = [sys.executable, "-m", "pip", "install", "--upgrade", "pyruns"]
    print(f"[pyruns] Running: {subprocess.list2cmdline(command)}", flush=True)
    try:
        completed = subprocess.run(command, check=False)
        exit_code = int(completed.returncode)
    except OSError as exc:
        exit_code = 1
        print(f"[pyruns] Could not start pip: {exc}", file=sys.stderr, flush=True)

    installed_version = _query_installed_version(previous_version)
    ok = exit_code == 0
    if ok:
        print(f"[pyruns] Pyruns upgrade completed: {installed_version}", flush=True)
    else:
        print(
            f"[pyruns] Pyruns upgrade failed with exit code {exit_code}; restarting the available version.",
            file=sys.stderr,
            flush=True,
        )
    return {
        "ok": ok,
        "previous_version": str(previous_version),
        "installed_version": installed_version,
        "exit_code": exit_code,
    }


def relaunch_ui(*, port: int, token: str, result: dict[str, Any]) -> None:
    """Replace the updater with a clean interpreter running the installed UI."""

    command = [
        sys.executable,
        "-m",
        "pyruns.web.app",
        "--port",
        str(int(port)),
        "--no-browser",
    ]
    environment = os.environ.copy()
    environment[UI_PRODUCTION_RESTART_ENV] = "1"
    environment[UI_TOKEN_ENV] = str(token)
    environment[UI_UPDATE_RESULT_ENV] = json.dumps(result, ensure_ascii=True)
    os.execve(sys.executable, command, environment)


def _parse_args(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m pyruns.web.self_update")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--previous-version", required=True)
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> int:
    options = _parse_args(list(sys.argv[1:] if args is None else args))
    token = str(os.environ.pop(UI_TOKEN_ENV, "") or "")
    if not token:
        token = secrets.token_urlsafe(32)
    os.environ.pop(UI_PRODUCTION_RESTART_ENV, None)
    os.environ.pop(UI_UPDATE_RESULT_ENV, None)
    result = run_pip_upgrade(options.previous_version)
    relaunch_ui(port=options.port, token=token, result=result)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
