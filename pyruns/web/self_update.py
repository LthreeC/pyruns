"""Full-process Pyruns upgrade and UI restart helpers."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import subprocess
import sys
import tempfile
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
from pyruns.utils.process_utils import hidden_subprocess_kwargs


UI_PRODUCTION_RESTART_ENV = "PYRUNS_UI_PRODUCTION_RESTART"
UI_TOKEN_ENV = "PYRUNS_UI_TOKEN"
UI_UPDATE_RESULT_ENV = "PYRUNS_UI_UPDATE_RESULT"
PYPI_PROJECT_JSON_URL = "https://pypi.org/pypi/pyruns/json"
PYPI_CHECK_TIMEOUT_SECONDS = 5.0
PYPI_MAX_RESPONSE_BYTES = 1_048_576
UPDATE_PARTICIPANT_WAIT_SECONDS = 60.0
UPDATE_STATUS_REPORT_SECONDS = 5.0
SCHEDULED_OPERATION_RETRY_SECONDS = 10.0
PIP_UPGRADE_TIMEOUT_SECONDS = 300.0
COORDINATED_UPDATE_WAIT_SECONDS = 420.0
LOCAL_RELEASE_WAIT_SECONDS = 60.0
INSTALLED_VERSION_QUERY_TIMEOUT_SECONDS = 15.0
_WINDOWS_PROCESS_REPLACEMENT = os.name == "nt"


class ActiveTasksError(RuntimeError):
    """Raised when an upgrade is requested while tasks are still active."""

    def __init__(self, active_count: int) -> None:
        self.active_count = max(1, int(active_count))
        super().__init__(
            f"Pyruns can update or restart only while idle; {self.active_count} queued or running "
            f"task{'s' if self.active_count != 1 else ''} remain. "
            "Wait for them to finish, then retry. No update or restart has been scheduled."
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
        self._instance_lock = threading.Lock()
        self._requested = False
        self._shutdown_triggered = False
        self._runtime: Any = None
        self._current_version = str(current_version or "")
        self._installed_version_value = self._current_version
        self._target_version = ""
        self._request_id = ""
        self._operation = "upgrade"
        self._role = "owner"
        self._scheduled_operation: dict[str, str] | None = None
        self._next_scheduled_check = 0.0
        self._instance_id = secrets.token_hex(16)
        self._store = CoordinationStore(state_dir) if shared else None
        self._coordination_error = ""
        self._coordination_degraded = False
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._next_version_check = 0.0
        self._next_shared_restart_check = 0.0

    @property
    def supported(self) -> bool:
        return self._store is None or not self._coordination_error

    @property
    def requested(self) -> bool:
        with self._lock:
            return self._requested

    @property
    def state(self) -> str:
        return self.operation_status()[0]

    def operation_status(self) -> tuple[str, dict[str, str] | None]:
        with self._lock:
            pending = self._scheduled_operation
            if self._requested:
                state = "restarting"
            elif pending is not None and not pending.get("error"):
                state = "waiting"
            else:
                state = "restart_required" if self.restart_required else "idle"
            return state, pending.copy() if pending is not None else None

    def cancel_scheduled_operation(self, operation_id: str) -> bool:
        with self._lock:
            pending = self._scheduled_operation
            if (
                self._requested or pending is None or pending.get("error")
                or pending["id"] != operation_id
            ):
                return False
            self._scheduled_operation = None
            return True

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

    def _strict_active_count(self, *, restart_only: bool) -> int:
        try:
            for name in (
                ("strict_restart_active_task_count", "strict_active_task_count")
                if restart_only else ("strict_active_task_count",)
            ):
                if callable(getattr(type(self._runtime), name, None)):
                    return max(0, int(getattr(self._runtime, name)()))
            return max(0, int(self._runtime.active_task_count()))
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            raise UpdateCheckError(
                "Could not verify that every managed workspace is idle. "
                f"{detail[:1600]} "
                "Retry after task state settles; "
                "if this persists, check the reported workspace and file access."
            ) from exc

    def _serving_counts(self) -> tuple[int, int]:
        try:
            active = max(0, int(self._runtime.active_task_count()))
            restart_active = active
            if active and callable(getattr(type(self._runtime), "cached_restart_active_task_count", None)):
                restart_active = max(0, int(self._runtime.cached_restart_active_task_count()))
            return active, restart_active
        except Exception:
            # Another UI should not start replacing this process on a failed check.
            return 1, 1

    def _instance_payload(
        self,
        *,
        phase: str,
        active_count: int,
        restart_active_count: int | None = None,
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
        if restart_active_count is not None:
            payload["restart_active_count"] = max(0, int(restart_active_count))
        return payload

    def _write_instance(
        self,
        *,
        phase: str,
        active_count: int,
        restart_active_count: int | None = None,
        request_id: str = "",
    ) -> None:
        if self._store is None:
            return
        with self._instance_lock:
            # A monitor may finish I/O after its bounded join has expired.
            # Only the explicit handoff may publish once monitoring has stopped.
            if self._stop.is_set() and phase != "handoff":
                return
            self._store.write_record(
                "instances",
                self._instance_id,
                self._instance_payload(
                    phase=phase,
                    active_count=active_count,
                    restart_active_count=restart_active_count,
                    request_id=request_id,
                ),
            )

    def _join_request(self, request: dict[str, Any]) -> None:
        with self._lock:
            self._requested = True
            self._scheduled_operation = None
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
            active_count, restart_active_count = self._serving_counts()
            with self._store.locked():
                request = self._store.active_request_locked(recover=True)
                if request is not None:
                    self._join_request(request)
                self._write_instance(
                    phase="draining" if request is not None else "serving",
                    active_count=active_count,
                    restart_active_count=restart_active_count,
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
            if self._coordination_degraded:
                return
        self._start_monitor()

    def _start_monitor(self) -> None:
        if self._monitor is not None:
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
                        active_count, restart_active_count = self._serving_counts()
                        self._write_instance(
                            phase="serving",
                            active_count=active_count,
                            restart_active_count=restart_active_count,
                        )
                    except (OSError, ValueError):
                        pass
                lock_context.__exit__(*sys.exc_info())

    def prepare(self, runtime: Any, *, target_version: str = "", defer_if_busy: bool = False) -> bool:
        """Publish an upgrade gate after every registered process is idle."""

        target_version = str(target_version or "").strip()
        if target_version:
            try:
                target = Version(target_version)
                current = Version(self._current_version) if self._current_version else None
            except InvalidVersion as exc:
                raise UpdateCheckError("The requested Pyruns target version is invalid.") from exc
            if current is not None and target <= current:
                raise UpdateCheckError("The requested Pyruns target version is not newer.")
        return self._prepare_operation(
            runtime,
            operation="upgrade",
            target_version=target_version,
            defer_if_busy=defer_if_busy,
        )

    def prepare_restart(self, runtime: Any, *, defer_if_busy: bool = False) -> bool:
        """Publish a restart gate for an externally changed installation."""

        installed = self.refresh_installed_version(force=True)
        if not self._current_version or installed == self._current_version:
            raise UpdateCheckError("The running UI already matches the installed Pyruns version.")
        return self._prepare_operation(
            runtime,
            operation="restart",
            target_version=installed,
            defer_if_busy=defer_if_busy,
        )

    def _prepare_operation(
        self,
        runtime: Any,
        *,
        operation: str,
        target_version: str,
        defer_if_busy: bool,
    ) -> bool:
        with self._lock:
            if self._stop.is_set():
                raise UpdateCheckError("This Pyruns interface is already shutting down.")
            self._runtime = runtime
            pending = self._scheduled_operation
            if defer_if_busy and pending is not None and not pending.get("error"):
                if (operation, target_version) != (pending["operation"], pending["target_version"]):
                    raise UpdateCheckError("Cancel the scheduled Pyruns operation before scheduling another.")
                return False
            try:
                return self._admit_operation(operation, target_version)
            except ActiveTasksError:
                if not defer_if_busy:
                    raise
                self._scheduled_operation = {
                    "id": secrets.token_hex(16),
                    "operation": operation,
                    "target_version": target_version,
                }
                self._next_scheduled_check = time.monotonic() + SCHEDULED_OPERATION_RETRY_SECONDS
                self._start_monitor()
                return False

    def _admit_operation(self, operation: str, target_version: str) -> bool:
        """Check and publish the gate while the coordinator lock is held."""

        if self._requested:
            return self._strict_active_count(restart_only=self._operation == "restart") == 0
        if self._coordination_error:
            raise UpdateCheckError(
                "Shared update coordination is unavailable for this installation."
            )
        restart_only = operation == "restart"
        if self._store is None:
            active_count = self._strict_active_count(restart_only=restart_only)
            if active_count:
                raise ActiveTasksError(active_count)
            if self._stop.is_set():
                raise UpdateCheckError("This Pyruns interface is already shutting down.")
            self._requested = True
            self._scheduled_operation = None
            self._operation = operation
            self._target_version = target_version
            self._role = "owner"
            return True
        try:
            with self._store.locked():
                existing = self._store.active_request_locked(recover=True)
                if existing is not None:
                    self._join_request(existing)
                    active_count = self._strict_active_count(restart_only=self._operation == "restart")
                    self._write_instance(phase="draining", active_count=active_count, request_id=self._request_id)
                    return active_count == 0

                instances = self._store.live_records_locked("instances")
                count_key = "restart_active_count" if restart_only else "active_count"
                active_count = sum(
                    max(0, int(item.get(count_key, item.get("active_count", 0)) or 0))
                    for item in instances
                    if str(item.get("id", "") or "") != self._instance_id
                )
                if not restart_only:
                    active_count += len(self._store.live_records_locked("activities"))
                if active_count:
                    raise ActiveTasksError(active_count)
                active_count = self._strict_active_count(restart_only=restart_only)
                if active_count:
                    raise ActiveTasksError(active_count)
                if self._stop.is_set():
                    raise UpdateCheckError("This Pyruns interface is already shutting down.")

                request_id = secrets.token_hex(16)
                request = process_record(
                    record_id=self._instance_id,
                    kind="ui-restart" if restart_only else "ui-update",
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
                self._write_instance(phase="draining", active_count=0, request_id=request_id)
                return True
        except (OSError, UpdateCoordinationError, TypeError, ValueError) as exc:
            raise UpdateCheckError(
                f"Could not coordinate an idle {operation} across this Pyruns installation."
            ) from exc

    def _retry_scheduled_operation(self, serving_counts: tuple[int, int] | None = None) -> None:
        with self._lock:
            pending = self._scheduled_operation
            now = time.monotonic()
            if (
                self._stop.is_set() or self._coordination_error or pending is None
                or pending.get("error") or now < self._next_scheduled_check
            ):
                return
            operation = pending["operation"]
            target_version = pending["target_version"]
            self._next_scheduled_check = now + SCHEDULED_OPERATION_RETRY_SECONDS
            try:
                installed = self.refresh_installed_version(force=True)
                if operation == "restart" and installed != target_version:
                    raise UpdateCheckError("The installed Pyruns version changed while the restart was waiting.")
                if operation == "upgrade" and target_version and installed and installed != self._current_version:
                    installed_version = Version(installed)
                    confirmed_version = Version(target_version)
                    if installed_version > confirmed_version:
                        raise UpdateCheckError("A newer Pyruns version was installed while the update was waiting.")
                    if installed_version == confirmed_version:
                        operation = "restart"
                if serving_counts is not None:
                    active, restart_active = serving_counts
                    if (restart_active if operation == "restart" else active) > 0:
                        return
                ready = self._admit_operation(operation, target_version)
            except ActiveTasksError:
                return
            except (UpdateCheckError, InvalidVersion) as exc:
                if self._scheduled_operation is pending:
                    pending["error"] = str(exc)
                return
        if ready:
            self.trigger_shutdown()

    def trigger_shutdown(self) -> None:
        """Ask Uvicorn to finish after the update response has been sent."""

        with self._lock:
            if self._shutdown_triggered or self._stop.is_set():
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
        active_count = self._strict_active_count(restart_only=self._operation == "restart")
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

    def _restart_after_shared_completion(self, installed: str) -> bool:
        """Join a successful shared update this UI missed while it was serving."""

        if self._store is None:
            return False
        with self._lock:
            now = time.monotonic()
            scheduled = self._scheduled_operation
            if (
                self._requested or self._stop.is_set() or now < self._next_shared_restart_check
                or (scheduled is not None and not scheduled.get("error"))
            ):
                return False
            self._next_shared_restart_check = now + SCHEDULED_OPERATION_RETRY_SECONDS
            with self._store.locked():
                request = self._store.read_request_locked()
                if request is None or request.get("stage") != "completed" or not request.get("request_id"):
                    return False
                result = request.get("result")
                if (
                    not isinstance(result, dict) or result.get("ok") is not True
                    or result.get("installed_version") != installed
                ):
                    return False
                try:
                    if self._strict_active_count(restart_only=True):
                        return False
                except UpdateCheckError:
                    return False
                if self._stop.is_set():
                    return False
                # Reuse the follower handoff with the existing completed request.
                # Its restart must not publish a new gate or restart updated peers.
                self._join_request(request)
                self._role = "follower"
                self._operation = "restart"
                self._target_version = installed
                return True

    def _monitor_tick(self) -> None:
        if self._store is None:
            self._retry_scheduled_operation()
            return
        # Match prepare's lock order so a completed request cannot clear a
        # newly accepted update while this monitor is reading shared state.
        with self._lock:
            with self._store.locked():
                request = self._store.active_request_locked(recover=True)
                if request is not None:
                    self._join_request(request)
                elif self._requested and not self._shutdown_triggered:
                    completed = self._store.read_request_locked()
                    # Missing/stale NFS reads are not proof that pip stopped.
                    if (
                        completed is not None
                        and completed.get("request_id") == self._request_id
                        and completed.get("stage") == "completed"
                    ):
                        self._requested = False
                        self._request_id = ""
                        self._target_version = ""
        if request is not None:
            self._heartbeat_request_if_owner()
            self._drain_once()
            return

        serving_counts = self._serving_counts()
        self._write_instance(
            phase="serving",
            active_count=serving_counts[0],
            restart_active_count=serving_counts[1],
        )
        if self._current_version:
            installed = self.refresh_installed_version()
            if (
                installed != self._current_version and not serving_counts[1]
                and self._restart_after_shared_completion(installed)
            ):
                self.trigger_shutdown()
                return
        self._retry_scheduled_operation(serving_counts)

    def _monitor_loop(self) -> None:
        while not self._stop.wait(UPDATE_HEARTBEAT_SECONDS):
            try:
                self._monitor_tick()
            except (OSError, RuntimeError, TypeError, UpdateCoordinationError, ValueError) as exc:
                with self._lock:
                    self._coordination_error = str(exc) or (
                        "Shared update coordination became unavailable."
                    )
            else:
                with self._lock:
                    self._coordination_error = ""

    def handoff(self) -> dict[str, Any]:
        """Stop monitoring and leave a waiter/updater lease for process replacement."""

        self._stop_monitor()
        self._heartbeat_request_if_owner()
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
            with self._instance_lock:
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
    except (OSError, TimeoutError, ValueError, urllib.error.URLError, http.client.HTTPException) as exc:
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


def read_update_result(*, running_version: str = "") -> dict[str, Any] | None:
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
    except (TypeError, ValueError, OverflowError):
        return None
    result = {
        "ok": payload["ok"],
        "previous_version": str(payload.get("previous_version", "") or ""),
        "installed_version": str(payload.get("installed_version", "") or ""),
        "exit_code": exit_code,
    }
    if isinstance(payload.get("error"), str) and payload["error"]:
        result["error"] = payload["error"][-1024:]
    if result["ok"] and running_version and result["installed_version"] != running_version:
        expected = result["installed_version"]
        result.update(
            ok=False,
            installed_version=running_version,
            exit_code=1,
            error=f"This host started Pyruns {running_version} after the shared update reported {expected}.",
        )
    return result


def _replace_current_process(command: list[str], environment: dict[str, str]) -> None:
    """Run a replacement interpreter without losing its inherited terminal."""

    if _WINDOWS_PROCESS_REPLACEMENT:
        # Windows has no reliable in-place exec here; waiting keeps `pyr ui`
        # attached to the replacement process and preserves its terminal output.
        try:
            exit_code = subprocess.call(
                command,
                env=environment,
                **hidden_subprocess_kwargs(),
            )
        except OSError as exc:
            raise RuntimeError(f"Could not start the replacement Pyruns process: {exc}") from exc
        raise SystemExit(exit_code)
    os.execve(sys.executable, command, environment)


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
    target_version: str = "",
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
    if target_version:
        command.extend(["--target-version", str(target_version)])
    environment = os.environ.copy()
    environment[UI_TOKEN_ENV] = str(token)
    message = (
        "[pyruns] Shared Pyruns files changed; restarting every idle UI."
        if restart_only
        else "[pyruns] Every Pyruns UI is idle; starting the shared installation upgrade."
    )
    print(message, flush=True)
    _replace_current_process(command, environment)


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
    _replace_current_process(command, environment)


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
            timeout=INSTALLED_VERSION_QUERY_TIMEOUT_SECONDS,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return fallback
    version_text = str(completed.stdout or "").strip()
    return version_text if completed.returncode == 0 and version_text else fallback


def run_pip_upgrade(
    previous_version: str,
    *,
    target_version: str = "",
) -> dict[str, Any]:
    """Run the requested pip operation and return a relaunch-safe result."""

    requirement = f"pyruns>={Version(target_version)}" if target_version else "pyruns"
    command = [
        sys.executable, "-m", "pip", "install", "--upgrade",
        "--no-input", "--disable-pip-version-check", requirement,
    ]
    error = ""
    print(f"[pyruns] Running: {subprocess.list2cmdline(command)}", flush=True)
    try:
        # A file keeps error output bounded in memory and avoids waiting for
        # inherited pipe handles in pip's build children after a timeout.
        with tempfile.TemporaryFile() as error_log:
            completed = subprocess.run(
                command,
                check=False,
                timeout=PIP_UPGRADE_TIMEOUT_SECONDS,
                stderr=error_log,
                **hidden_subprocess_kwargs(),
            )
            error_log.seek(0, os.SEEK_END)
            error_log.seek(max(0, error_log.tell() - 8192))
            stderr = error_log.read().decode("utf-8", errors="replace").strip()
        exit_code = int(completed.returncode)
        if stderr:
            print(stderr, file=sys.stderr, flush=True)
            if exit_code:
                error = stderr[-1024:]
    except subprocess.TimeoutExpired:
        exit_code = 124
        error = f"pip did not finish within {PIP_UPGRADE_TIMEOUT_SECONDS:g} seconds."
        print(
            f"[pyruns] pip did not finish within {PIP_UPGRADE_TIMEOUT_SECONDS:g} seconds; "
            "restarting the available version.",
            file=sys.stderr,
            flush=True,
        )
    except OSError as exc:
        exit_code = 1
        error = f"Could not start pip: {exc}"
        print(f"[pyruns] Could not start pip: {exc}", file=sys.stderr, flush=True)

    installed_version = _query_installed_version(previous_version)
    try:
        previous = Version(str(previous_version))
        installed = Version(str(installed_version))
        target = Version(str(target_version)) if target_version else None
        reached_target = installed > previous and (target is None or installed >= target)
    except InvalidVersion:
        reached_target = False
    ok = exit_code == 0 and reached_target
    if ok:
        print(f"[pyruns] Pyruns upgrade completed: {installed_version}", flush=True)
    elif exit_code == 0:
        expected = (
            f"at least {target_version}"
            if target_version
            else f"newer than {previous_version}"
        )
        print(
            f"[pyruns] pip completed, but installed Pyruns {installed_version}; expected {expected}.",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(
            f"[pyruns] Pyruns upgrade failed with exit code {exit_code}; restarting the available version.",
            file=sys.stderr,
            flush=True,
        )
    result = {
        "ok": ok,
        "previous_version": str(previous_version),
        "installed_version": installed_version,
        "exit_code": exit_code,
    }
    if error:
        result["error"] = error[-1024:]
    return result


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
                runner_device=owner.get("runner_device"),
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
    restart_only: bool = False,
) -> None:
    """Wait until the UIs stop and, for a pip upgrade, CLI activity has ended."""

    deadline = time.monotonic() + UPDATE_PARTICIPANT_WAIT_SECONDS
    next_report = 0.0
    stopped_phases = {"handoff", "waiting"} if restart_only else {"waiting"}
    while True:
        with store.locked():
            request = store.read_request_locked()
            if request is None or str(request.get("request_id", "") or "") != request_id:
                raise UpdateCoordinationError("The shared Pyruns update request disappeared.")
            owner = process_record(record_id=instance_id, kind="ui-update")
            ownership = {
                "stage": "draining",
                "heartbeat_at": time.time(),
                "host": owner["host"],
                "runner_device": owner.get("runner_device"),
                "pid": owner["pid"],
                "process_create_time": owner["process_create_time"],
            }
            if store.request_is_active(request):
                store.update_request_locked(request_id, **ownership)
            elif store.resume_recovered_request_locked(
                request_id,
                instance_id,
                **ownership,
            ) is None:
                raise UpdateCoordinationError(
                    "The shared Pyruns update request is no longer active."
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
            activities = [] if restart_only else store.live_records_locked("activities")
            blockers = [
                item
                for item in instances
                if str(item.get("id", "") or "") != instance_id
                and not (
                    str(item.get("request_id", "") or "") == request_id
                    and str(item.get("phase", "") or "") in stopped_phases
                )
            ]
        if not blockers and not activities:
            return
        now = time.monotonic()
        blocked = [*blockers, *activities]
        details = ", ".join(
            f"{item.get('kind', 'process')}@{item.get('host', 'unknown')} "
            f"pid={item.get('pid', '?')} phase={item.get('phase', 'active')}"
            for item in blocked[:8]
        )
        if len(blocked) > 8:
            details = f"{details}, and {len(blocked) - 8} more"
        if now >= next_report:
            print(
                f"[pyruns] Waiting for {len(blocked)} shared Pyruns process"
                f"{'es' if len(blocked) != 1 else ''}: {details}",
                flush=True,
            )
            next_report = now + UPDATE_STATUS_REPORT_SECONDS
        if now >= deadline:
            raise UpdateCoordinationError(
                f"Timed out after {UPDATE_PARTICIPANT_WAIT_SECONDS:g} seconds waiting for "
                f"shared Pyruns processes to stop: {details}"
            )
        time.sleep(min(0.2, max(0.0, deadline - now)))


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
        active = _heartbeat_coordinated_process(
            self.store,
            request_id=self.request_id,
            instance_id=self.instance_id,
            phase="updater",
            version=self.previous_version,
            stage=self.stage,
        )
        if not active:
            raise UpdateCoordinationError(
                "The shared Pyruns update request is no longer active."
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
    target_version: str = "",
) -> dict[str, Any]:
    """Drain shared users, run pip once, and publish the result atomically."""

    store = CoordinationStore(state_dir)
    _wait_for_update_participants(
        store,
        request_id=request_id,
        instance_id=instance_id,
        previous_version=previous_version,
        restart_only=restart_only,
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
            result = run_pip_upgrade(
                previous_version,
                target_version=target_version,
            )

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
    """Keep a follower lease alive until completion and local release visibility."""

    store = CoordinationStore(state_dir)
    deadline = time.monotonic() + COORDINATED_UPDATE_WAIT_SECONDS
    next_report = time.monotonic() + UPDATE_STATUS_REPORT_SECONDS
    release_deadline: float | None = None
    while True:
        request = None
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
            pass
        # A missing or older request can be a stale shared-filesystem read.
        # Only completion for this handoff lets a follower relaunch early.
        if (
            request is not None
            and str(request.get("request_id", "") or "") == request_id
            and str(request.get("stage", "") or "") == "completed"
        ):
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
                error = raw_result.get("error") or request.get("error")
                if isinstance(error, str) and error:
                    result["error"] = error[-1024:]
            else:
                result = {
                    "ok": False,
                    "previous_version": str(previous_version),
                    "installed_version": str(previous_version),
                    "exit_code": 1,
                }
            if not result["ok"]:
                break
            # Seeing the completion marker on NFS does not prove this client
            # can see the updated package metadata yet.
            expected = result["installed_version"]
            installed = _query_installed_version("")
            if installed and installed == expected:
                break
            now = time.monotonic()
            if release_deadline is None:
                release_deadline = min(deadline, now + LOCAL_RELEASE_WAIT_SECONDS)
                print(f"[pyruns] Waiting for this host to see Pyruns {expected} on disk.", flush=True)
            if now >= release_deadline:
                result.update(
                    ok=False,
                    installed_version=installed or previous_version,
                    exit_code=124,
                    error=f"This host could not verify Pyruns {expected} on disk after the shared update.",
                )
                break
            time.sleep(min(UPDATE_HEARTBEAT_SECONDS, release_deadline - now))
            continue
        now = time.monotonic()
        if now >= (release_deadline if release_deadline is not None else deadline):
            print(
                f"[pyruns] Shared Pyruns update did not finish within "
                f"{COORDINATED_UPDATE_WAIT_SECONDS:g} seconds; restarting the available version.",
                file=sys.stderr,
                flush=True,
            )
            result = {
                "ok": False,
                "previous_version": str(previous_version),
                "installed_version": _query_installed_version(previous_version),
                "exit_code": 124,
            }
            break
        if now >= next_report:
            print("[pyruns] Still waiting for the shared Pyruns update to finish.", flush=True)
            next_report = now + UPDATE_STATUS_REPORT_SECONDS
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
    _replace_current_process(command, environment)


def _coordination_failure_result(previous_version: str, error: str = "") -> dict[str, Any]:
    result = {
        "ok": False,
        "previous_version": str(previous_version),
        "installed_version": _query_installed_version(previous_version),
        "exit_code": 1,
    }
    if error:
        result["error"] = error[-1024:]
    return result


def _finish_failed_coordination(
    *,
    state_dir: str,
    request_id: str,
    instance_id: str,
    result: dict[str, Any],
    error: str,
) -> None:
    store = CoordinationStore(state_dir)
    try:
        with store.locked():
            store.update_request_locked(
                request_id,
                stage="completed",
                heartbeat_at=time.time(),
                completed_at=time.time(),
                result=result,
                error=error,
            )
        store.remove_record("instances", instance_id)
    except (OSError, UpdateCoordinationError, ValueError):
        pass


def restart_ui_after_handoff(
    *,
    port: int,
    token: str,
    previous_version: str,
    request_id: str,
    instance_id: str,
    state_dir: str,
    installed_version: str,
    owner: bool,
) -> None:
    """Finish a restart in loaded code, then launch the on-disk version."""

    print(
        "[pyruns] Coordinating the shared Pyruns interface restart."
        if owner
        else "[pyruns] Waiting for the shared Pyruns interface restart.",
        flush=True,
    )
    try:
        if owner:
            result = run_coordinated_update(
                state_dir=state_dir,
                request_id=request_id,
                instance_id=instance_id,
                previous_version=previous_version,
                restart_only=True,
                installed_version=installed_version,
            )
        else:
            result = wait_for_coordinated_update(
                state_dir=state_dir,
                request_id=request_id,
                instance_id=instance_id,
                previous_version=previous_version,
            )
    except Exception as exc:
        print(f"[pyruns] Restart coordination failed: {exc}", file=sys.stderr, flush=True)
        result = _coordination_failure_result(previous_version, str(exc))
        _finish_failed_coordination(
            state_dir=state_dir,
            request_id=request_id,
            instance_id=instance_id,
            result=result,
            error=str(exc),
        )
    relaunch_ui(port=port, token=token, result=result)


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
    parser.add_argument("--target-version", default="")
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
                    target_version=str(options.target_version or ""),
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
            result = run_pip_upgrade(
                options.previous_version,
                target_version=str(options.target_version or ""),
            )
    except Exception as exc:
        print(f"[pyruns] Update coordination failed: {exc}", file=sys.stderr, flush=True)
        result = _coordination_failure_result(options.previous_version, str(exc))
        if options.request_id and options.instance_id and options.state_dir:
            _finish_failed_coordination(
                state_dir=options.state_dir,
                request_id=options.request_id,
                instance_id=options.instance_id,
                result=result,
                error=str(exc),
            )
    relaunch_ui(port=options.port, token=token, result=result)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
