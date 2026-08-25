"""Full-process Pyruns upgrade and UI restart helpers."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version as distribution_version
from typing import Any, Callable, Iterator

from packaging.version import InvalidVersion, Version

from pyruns.update_coordination import (
    UPDATE_HEARTBEAT_SECONDS,
    CoordinationStore,
    UpdateCoordinationError,
    UpdateInProgressError,
    _coordination_write_is_denied,
    process_record,
)


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
            f"Pyruns can update or restart only while idle; {self.active_count} queued or running "
            f"task{'s' if self.active_count != 1 else ''} remain."
        )


class UpdateCheckError(RuntimeError):
    """Raised when Pyruns cannot establish that every managed workspace is idle."""


class LatestVersionCheckError(RuntimeError):
    """Raised when the latest published Pyruns version cannot be verified."""


class UiUpdateCoordinator:
    """Gate starts and coordinate idle restarts across one shared installation."""

    def __init__(
        self,
        shutdown_callback: Callable[[], None],
        *,
        state_dir: str | None = None,
        shared: bool = False,
        current_version: str = "",
    ) -> None:
        self._shutdown_callback = shutdown_callback
        self._lock = threading.RLock()
        self._requested = False
        self._shutdown_triggered = False
        self._runtime: Any = None
        self._current_version = str(current_version or "")
        self._installed_version_value = self._current_version
        self._target_version = ""
        self._request_id = ""
        self._operation = "upgrade"
        self._role = "owner"
        self._instance_id = secrets.token_hex(16)
        self._store = CoordinationStore(state_dir) if shared else None
        self._coordination_error = ""
        self._coordination_degraded = False
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._next_version_check = 0.0

    @property
    def supported(self) -> bool:
        return self._store is None or not self._coordination_error

    @property
    def requested(self) -> bool:
        with self._lock:
            return self._requested

    @property
    def state(self) -> str:
        if self.requested:
            return "restarting"
        return "restart_required" if self.restart_required else "idle"

    @property
    def installed_version(self) -> str:
        with self._lock:
            return self._installed_version_value or self._current_version

    @property
    def restart_required(self) -> bool:
        with self._lock:
            return bool(
                self._current_version
                and self._installed_version_value
                and self._installed_version_value != self._current_version
            )

    def refresh_installed_version(self, *, force: bool = False) -> str:
        """Refresh the on-disk distribution version without requesting a restart."""

        now = time.monotonic()
        with self._lock:
            if not force and now < self._next_version_check:
                return self._installed_version_value or self._current_version
            self._next_version_check = now + 5.0
        installed = self._installed_version(self._current_version)
        with self._lock:
            self._installed_version_value = installed
            return installed

    @property
    def instance_id(self) -> str:
        return self._instance_id

    def _runtime_active_count(self, *, strict: bool) -> int:
        try:
            return max(0, int(self._runtime.active_task_count()))
        except Exception as exc:
            if strict:
                raise UpdateCheckError(
                    "Could not verify that every managed workspace is idle."
                ) from exc
            return 1

    def _instance_payload(
        self,
        *,
        phase: str,
        active_count: int,
        request_id: str = "",
    ) -> dict[str, Any]:
        payload = process_record(record_id=self._instance_id, kind="ui")
        payload.update(
            {
                "phase": str(phase),
                "active_count": max(0, int(active_count)),
                "request_id": str(request_id),
                "version": self._current_version,
            }
        )
        return payload

    def _write_instance(
        self,
        *,
        phase: str,
        active_count: int,
        request_id: str = "",
    ) -> None:
        if self._store is None:
            return
        self._store.write_record(
            "instances",
            self._instance_id,
            self._instance_payload(
                phase=phase,
                active_count=active_count,
                request_id=request_id,
            ),
        )

    def _join_request(self, request: dict[str, Any]) -> None:
        with self._lock:
            self._requested = True
            self._request_id = str(request.get("request_id", "") or "")
            self._operation = str(request.get("operation", "upgrade") or "upgrade")
            self._target_version = str(request.get("target_version", "") or "")
            self._role = (
                "owner"
                if str(request.get("owner_instance_id", "") or "") == self._instance_id
                else "follower"
            )

    def attach(self, runtime: Any) -> None:
        """Register a production UI and begin watching shared update state."""

        self._runtime = runtime
        if self._store is None or self._monitor is not None:
            return
        try:
            self._store.ensure()
            active_count = self._runtime_active_count(strict=False)
            with self._store.locked():
                request = self._store.active_request_locked(recover=True)
                if request is not None:
                    self._join_request(request)
                self._write_instance(
                    phase="draining" if request is not None else "serving",
                    active_count=active_count,
                    request_id=self._request_id,
                )
        except (OSError, UpdateCoordinationError, ValueError) as exc:
            self._coordination_error = str(exc)
            if isinstance(exc, OSError) and _coordination_write_is_denied(exc):
                try:
                    request = self._store.read_request_locked(
                        strict=True,
                        allow_missing=True,
                    )
                except UpdateCoordinationError:
                    request = None
                    self._coordination_error = (
                        "Shared update coordination could not be read safely."
                    )
                if (
                    request is not None
                    and self._store.request_is_active(request)
                ):
                    self._coordination_degraded = False
                elif self._coordination_error != (
                    "Shared update coordination could not be read safely."
                ):
                    self._coordination_degraded = True
            return
        self._monitor = threading.Thread(
            target=self._monitor_loop,
            name="pyruns-update-monitor",
            daemon=True,
        )
        self._monitor.start()

    @contextmanager
    def task_start_guard(self) -> Iterator[None]:
        """Serialize a task claim against publishing the installation update gate."""

        with self._lock:
            if self._requested:
                raise UpdateInProgressError(
                    "Pyruns is updating; new tasks are disabled until every UI restarts."
                )
            if self._store is None or self._coordination_degraded:
                yield
                return
            if self._coordination_error:
                raise UpdateInProgressError(
                    "Could not coordinate this task start with the shared Pyruns installation."
                )
            lock_context = self._store.locked()
            lock_acquired = False
            try:
                lock_context.__enter__()
                lock_acquired = True
                if self._store.active_request_locked(recover=True) is not None:
                    raise UpdateInProgressError(
                        "Pyruns is updating; new tasks are disabled until every UI restarts."
                    )
            except UpdateInProgressError:
                if lock_acquired:
                    lock_context.__exit__(*sys.exc_info())
                raise
            except (OSError, UpdateCoordinationError, ValueError) as exc:
                if lock_acquired:
                    lock_context.__exit__(*sys.exc_info())
                raise UpdateInProgressError(
                    "Could not coordinate this task start with the shared Pyruns installation."
                ) from exc
            try:
                yield
            finally:
                if sys.exc_info()[0] is None:
                    try:
                        self._write_instance(
                            phase="serving",
                            active_count=self._runtime_active_count(strict=False),
                        )
                    except (OSError, ValueError):
                        pass
                lock_context.__exit__(*sys.exc_info())

    def prepare(self, runtime: Any) -> bool:
        """Publish an upgrade gate after every registered process is idle."""

        return self._prepare_operation(runtime, operation="upgrade", target_version="")

    def prepare_restart(self, runtime: Any) -> bool:
        """Publish a restart gate for an externally changed installation."""

        installed = self.refresh_installed_version(force=True)
        if not self._current_version or installed == self._current_version:
            raise UpdateCheckError("The running UI already matches the installed Pyruns version.")
        return self._prepare_operation(
            runtime,
            operation="restart",
            target_version=installed,
        )

    def _prepare_operation(
        self,
        runtime: Any,
        *,
        operation: str,
        target_version: str,
    ) -> bool:
        """Publish one shared full-process operation after every user is idle."""

        with self._lock:
            if self._requested:
                return self._runtime_active_count(strict=True) == 0
            self._runtime = runtime
            if self._coordination_error:
                raise UpdateCheckError(
                    "Shared update coordination is unavailable for this installation."
                )
            if self._store is None:
                active_count = self._runtime_active_count(strict=True)
                if active_count > 0:
                    raise ActiveTasksError(active_count)
                self._requested = True
                self._operation = operation
                self._target_version = target_version
                self._role = "owner"
                return True
            try:
                with self._store.locked():
                    existing = self._store.active_request_locked(recover=True)
                    if existing is not None:
                        self._join_request(existing)
                        active_count = self._runtime_active_count(strict=True)
                        self._write_instance(
                            phase="draining",
                            active_count=active_count,
                            request_id=self._request_id,
                        )
                        return active_count == 0

                    local_count = self._runtime_active_count(strict=True)
                    instances = self._store.live_records_locked("instances")
                    activities = self._store.live_records_locked("activities")
                    remote_count = sum(
                        max(0, int(item.get("active_count", 0) or 0))
                        for item in instances
                        if str(item.get("id", "") or "") != self._instance_id
                    )
                    active_count = local_count + remote_count + len(activities)
                    if active_count > 0:
                        raise ActiveTasksError(active_count)

                    request_id = secrets.token_hex(16)
                    request = process_record(
                        record_id=self._instance_id,
                        kind="ui-restart" if operation == "restart" else "ui-update",
                    )
                    request.update(
                        {
                            "request_id": request_id,
                            "owner_instance_id": self._instance_id,
                            "operation": operation,
                            "stage": "draining",
                            "previous_version": self._current_version,
                            "target_version": target_version,
                            "requested_at": time.time(),
                        }
                    )
                    self._store.write_request_locked(request)
                    self._join_request(request)
                    self._write_instance(
                        phase="draining",
                        active_count=0,
                        request_id=request_id,
                    )
                    return True
            except ActiveTasksError:
                raise
            except UpdateCheckError:
                raise
            except (OSError, UpdateCoordinationError, TypeError, ValueError) as exc:
                raise UpdateCheckError(
                    f"Could not coordinate an idle {operation} across this Pyruns installation."
                ) from exc

    def trigger_shutdown(self) -> None:
        """Ask Uvicorn to finish after the update response has been sent."""

        with self._lock:
            if self._shutdown_triggered:
                return
            self._shutdown_triggered = True
        try:
            self._shutdown_callback()
        except BaseException:
            with self._lock:
                self._shutdown_triggered = False
            raise

    @staticmethod
    def _installed_version(fallback: str) -> str:
        try:
            value = str(distribution_version("pyruns") or "").strip()
        except (PackageNotFoundError, OSError, ValueError):
            return fallback
        return value or fallback

    def _drain_once(self) -> None:
        active_count = self._runtime_active_count(strict=False)
        self._write_instance(
            phase="draining",
            active_count=active_count,
            request_id=self._request_id,
        )
        if active_count == 0:
            self.trigger_shutdown()

    def _heartbeat_request_if_owner(self) -> None:
        if self._store is None or self._role != "owner" or not self._request_id:
            return
        with self._store.locked():
            request = self._store.read_request_locked()
            if (
                request is not None
                and str(request.get("request_id", "") or "") == self._request_id
                and self._store.request_is_active(request)
            ):
                self._store.update_request_locked(
                    self._request_id,
                    heartbeat_at=time.time(),
                )

    def _monitor_tick(self) -> None:
        if self._store is None:
            return
        with self._store.locked():
            request = self._store.active_request_locked(recover=True)
        if request is not None:
            self._join_request(request)
            self._heartbeat_request_if_owner()
            self._drain_once()
            return

        active_count = self._runtime_active_count(strict=False)
        self._write_instance(phase="serving", active_count=active_count)
        if self._current_version:
            self.refresh_installed_version()

    def _monitor_loop(self) -> None:
        while not self._stop.wait(UPDATE_HEARTBEAT_SECONDS):
            try:
                self._monitor_tick()
            except (OSError, RuntimeError, TypeError, UpdateCoordinationError, ValueError) as exc:
                with self._lock:
                    self._coordination_error = str(exc) or (
                        "Shared update coordination became unavailable."
                    )
                return

    def handoff(self) -> dict[str, Any]:
        """Stop monitoring and leave a waiter/updater lease for process replacement."""

        self._stop_monitor()
        phase = "handoff"
        self._write_instance(
            phase=phase,
            active_count=0,
            request_id=self._request_id,
        )
        return {
            "role": self._role,
            "operation": self._operation,
            "request_id": self._request_id,
            "instance_id": self._instance_id,
            "state_dir": self._store.state_dir if self._store is not None else "",
            "target_version": self._target_version,
        }

    def _stop_monitor(self) -> None:
        self._stop.set()
        if self._monitor is not None and self._monitor is not threading.current_thread():
            self._monitor.join(timeout=2.0)
        self._monitor = None

    def quiesce(self) -> None:
        """Stop background checks before the owning runtime is shut down."""

        self._stop_monitor()

    def close(self, *, failed_handoff: bool = False) -> None:
        """Remove this process lease and recover a failed owner handoff."""

        self._stop_monitor()
        if self._store is None:
            return
        try:
            if failed_handoff and self._role == "owner" and self._request_id:
                with self._store.locked():
                    request = self._store.read_request_locked()
                    if (
                        request is not None
                        and str(request.get("request_id", "") or "") == self._request_id
                        and self._store.request_is_active(request)
                    ):
                        self._store.recover_stale_request_locked(request)
            self._store.remove_record("instances", self._instance_id)
        except (OSError, UpdateCoordinationError, ValueError):
            pass


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
    except (
        KeyError,
        TypeError,
        UnicodeError,
        ValueError,
        InvalidVersion,
        RecursionError,
    ) as exc:
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
    except (TypeError, ValueError, RecursionError):
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


def replace_process_with_updater(
    *,
    port: int,
    token: str,
    previous_version: str,
    request_id: str = "",
    instance_id: str = "",
    state_dir: str = "",
    restart_only: bool = False,
    installed_version: str = "",
) -> None:
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
    if request_id:
        command.extend(["--request-id", str(request_id)])
    if instance_id:
        command.extend(["--instance-id", str(instance_id)])
    if state_dir:
        command.extend(["--state-dir", str(state_dir)])
    if restart_only:
        command.append("--restart-only")
    if installed_version:
        command.extend(["--installed-version", str(installed_version)])
    environment = os.environ.copy()
    environment[UI_TOKEN_ENV] = str(token)
    message = (
        "[pyruns] Shared Pyruns files changed; restarting every idle UI."
        if restart_only
        else "[pyruns] Every Pyruns UI is idle; starting the shared installation upgrade."
    )
    print(message, flush=True)
    os.execve(sys.executable, command, environment)


def replace_process_with_waiter(
    *,
    port: int,
    token: str,
    previous_version: str,
    request_id: str,
    instance_id: str,
    state_dir: str,
) -> None:
    """Replace a stopped follower UI with a package-independent update waiter."""

    command = [
        sys.executable,
        "-m",
        "pyruns.web.self_update",
        "--wait",
        "--port",
        str(int(port)),
        "--previous-version",
        str(previous_version),
        "--request-id",
        str(request_id),
        "--instance-id",
        str(instance_id),
        "--state-dir",
        str(state_dir),
    ]
    environment = os.environ.copy()
    environment[UI_TOKEN_ENV] = str(token)
    print("[pyruns] Waiting for the shared Pyruns installation to finish updating.", flush=True)
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


def _coordinated_instance_payload(
    *,
    instance_id: str,
    request_id: str,
    phase: str,
    version: str,
) -> dict[str, Any]:
    payload = process_record(record_id=instance_id, kind="ui")
    payload.update(
        {
            "phase": str(phase),
            "active_count": 0,
            "request_id": str(request_id),
            "version": str(version),
        }
    )
    return payload


def _heartbeat_coordinated_process(
    store: CoordinationStore,
    *,
    request_id: str,
    instance_id: str,
    phase: str,
    version: str,
    stage: str | None = None,
) -> bool:
    with store.locked():
        request = store.read_request_locked()
        if request is None or str(request.get("request_id", "") or "") != request_id:
            return False
        if stage is not None and store.request_is_active(request):
            owner = process_record(record_id=instance_id, kind="ui-update")
            store.update_request_locked(
                request_id,
                stage=stage,
                heartbeat_at=time.time(),
                host=owner["host"],
                pid=owner["pid"],
                process_create_time=owner["process_create_time"],
            )
        store.write_record(
            "instances",
            instance_id,
            _coordinated_instance_payload(
                instance_id=instance_id,
                request_id=request_id,
                phase=phase,
                version=version,
            ),
        )
    return True


def _wait_for_update_participants(
    store: CoordinationStore,
    *,
    request_id: str,
    instance_id: str,
    previous_version: str,
) -> None:
    """Wait until every live UI is a waiter and every CLI runner has exited."""

    while True:
        with store.locked():
            request = store.read_request_locked()
            if request is None or str(request.get("request_id", "") or "") != request_id:
                raise UpdateCoordinationError("The shared Pyruns update request disappeared.")
            if not store.request_is_active(request):
                raise UpdateCoordinationError("The shared Pyruns update request is no longer active.")
            owner = process_record(record_id=instance_id, kind="ui-update")
            store.update_request_locked(
                request_id,
                stage="draining",
                heartbeat_at=time.time(),
                host=owner["host"],
                pid=owner["pid"],
                process_create_time=owner["process_create_time"],
            )
            store.write_record(
                "instances",
                instance_id,
                _coordinated_instance_payload(
                    instance_id=instance_id,
                    request_id=request_id,
                    phase="updater",
                    version=previous_version,
                ),
            )
            instances = store.live_records_locked("instances")
            activities = store.live_records_locked("activities")
            blockers = [
                item
                for item in instances
                if str(item.get("id", "") or "") != instance_id
                and not (
                    str(item.get("request_id", "") or "") == request_id
                    and str(item.get("phase", "") or "") == "waiting"
                )
            ]
        if not blockers and not activities:
            return
        time.sleep(0.2)


class _CoordinationHeartbeat:
    def __init__(
        self,
        store: CoordinationStore,
        *,
        request_id: str,
        instance_id: str,
        previous_version: str,
        stage: str,
    ) -> None:
        self.store = store
        self.request_id = request_id
        self.instance_id = instance_id
        self.previous_version = previous_version
        self.stage = stage
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name="pyruns-update-heartbeat",
            daemon=True,
        )

    def _run(self) -> None:
        while not self.stop_event.wait(UPDATE_HEARTBEAT_SECONDS):
            try:
                if not _heartbeat_coordinated_process(
                    self.store,
                    request_id=self.request_id,
                    instance_id=self.instance_id,
                    phase="updater",
                    version=self.previous_version,
                    stage=self.stage,
                ):
                    return
            except (OSError, UpdateCoordinationError, ValueError):
                continue

    def __enter__(self) -> "_CoordinationHeartbeat":
        _heartbeat_coordinated_process(
            self.store,
            request_id=self.request_id,
            instance_id=self.instance_id,
            phase="updater",
            version=self.previous_version,
            stage=self.stage,
        )
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2.0)


def run_coordinated_update(
    *,
    state_dir: str,
    request_id: str,
    instance_id: str,
    previous_version: str,
    restart_only: bool = False,
    installed_version: str = "",
) -> dict[str, Any]:
    """Drain shared users, run pip once, and publish the result atomically."""

    store = CoordinationStore(state_dir)
    _wait_for_update_participants(
        store,
        request_id=request_id,
        instance_id=instance_id,
        previous_version=previous_version,
    )
    with _CoordinationHeartbeat(
        store,
        request_id=request_id,
        instance_id=instance_id,
        previous_version=previous_version,
        stage="updating",
    ):
        if restart_only:
            result = {
                "ok": True,
                "previous_version": str(previous_version),
                "installed_version": str(installed_version or previous_version),
                "exit_code": 0,
            }
        else:
            result = run_pip_upgrade(previous_version)

    with store.locked():
        updated = store.update_request_locked(
            request_id,
            stage="completed",
            heartbeat_at=time.time(),
            completed_at=time.time(),
            result=result,
        )
        if updated is None:
            raise UpdateCoordinationError("Could not publish the Pyruns update result.")
    store.remove_record("instances", instance_id)
    return result


def wait_for_coordinated_update(
    *,
    state_dir: str,
    request_id: str,
    instance_id: str,
    previous_version: str,
) -> dict[str, Any]:
    """Keep a follower lease alive until the updater publishes completion."""

    store = CoordinationStore(state_dir)
    while True:
        try:
            _heartbeat_coordinated_process(
                store,
                request_id=request_id,
                instance_id=instance_id,
                phase="waiting",
                version=previous_version,
            )
            request = store.read_request()
        except (OSError, UpdateCoordinationError, ValueError):
            time.sleep(0.2)
            continue
        if request is None or str(request.get("request_id", "") or "") != request_id:
            result = {
                "ok": False,
                "previous_version": str(previous_version),
                "installed_version": str(previous_version),
                "exit_code": 1,
            }
            break
        if str(request.get("stage", "") or "") == "completed":
            raw_result = request.get("result")
            if isinstance(raw_result, dict) and isinstance(raw_result.get("ok"), bool):
                try:
                    exit_code = int(raw_result.get("exit_code", 1))
                except (TypeError, ValueError, OverflowError):
                    exit_code = 1
                result = {
                    "ok": bool(raw_result["ok"]),
                    "previous_version": str(
                        raw_result.get("previous_version", previous_version) or previous_version
                    ),
                    "installed_version": str(
                        raw_result.get("installed_version", previous_version) or previous_version
                    ),
                    "exit_code": exit_code,
                }
            else:
                result = {
                    "ok": False,
                    "previous_version": str(previous_version),
                    "installed_version": str(previous_version),
                    "exit_code": 1,
                }
            break
        time.sleep(0.2)
    store.remove_record("instances", instance_id)
    return result


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
    parser.add_argument("--request-id", default="")
    parser.add_argument("--instance-id", default="")
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--restart-only", action="store_true")
    parser.add_argument("--installed-version", default="")
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> int:
    options = _parse_args(list(sys.argv[1:] if args is None else args))
    token = str(os.environ.pop(UI_TOKEN_ENV, "") or "")
    if not token:
        token = secrets.token_urlsafe(32)
    os.environ.pop(UI_PRODUCTION_RESTART_ENV, None)
    os.environ.pop(UI_UPDATE_RESULT_ENV, None)
    try:
        coordinated = bool(
            options.request_id
            and options.instance_id
            and options.state_dir
        )
        if options.wait and not coordinated:
            raise UpdateCoordinationError(
                "A shared update waiter requires request, instance, and state identifiers."
            )
        if coordinated:
            if options.wait:
                result = wait_for_coordinated_update(
                    state_dir=options.state_dir,
                    request_id=options.request_id,
                    instance_id=options.instance_id,
                    previous_version=options.previous_version,
                )
            else:
                result = run_coordinated_update(
                    state_dir=options.state_dir,
                    request_id=options.request_id,
                    instance_id=options.instance_id,
                    previous_version=options.previous_version,
                    restart_only=bool(options.restart_only),
                    installed_version=str(options.installed_version or ""),
                )
        elif options.restart_only:
            result = {
                "ok": True,
                "previous_version": str(options.previous_version),
                "installed_version": str(
                    options.installed_version or options.previous_version
                ),
                "exit_code": 0,
            }
        else:
            result = run_pip_upgrade(options.previous_version)
    except Exception as exc:
        print(f"[pyruns] Update coordination failed: {exc}", file=sys.stderr, flush=True)
        result = {
            "ok": False,
            "previous_version": str(options.previous_version),
            "installed_version": _query_installed_version(options.previous_version),
            "exit_code": 1,
        }
        if options.request_id and options.instance_id and options.state_dir:
            store = CoordinationStore(options.state_dir)
            try:
                with store.locked():
                    store.update_request_locked(
                        options.request_id,
                        stage="completed",
                        heartbeat_at=time.time(),
                        completed_at=time.time(),
                        result=result,
                        error=str(exc),
                    )
                store.remove_record("instances", options.instance_id)
            except (OSError, UpdateCoordinationError, ValueError):
                pass
    relaunch_ui(port=options.port, token=token, result=result)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
