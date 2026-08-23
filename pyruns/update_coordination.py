"""Shared installation-level coordination for Pyruns updates and task starts."""

from __future__ import annotations

import json
import os
import secrets
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from pyruns.utils.process_utils import get_process_create_time, is_pid_running


UPDATE_STATE_DIR_ENV = "PYRUNS_UPDATE_STATE_DIR"
UPDATE_REQUEST_FILENAME = "request.json"
UPDATE_LOCK_FILENAME = "coordination.lock"
UPDATE_INSTANCES_DIRNAME = "instances"
UPDATE_ACTIVITIES_DIRNAME = "activities"
UPDATE_ACTIVE_STAGES = frozenset({"draining", "updating"})
UPDATE_HEARTBEAT_SECONDS = 1.0
UPDATE_LEASE_TIMEOUT_SECONDS = 60.0
UPDATE_REQUEST_TIMEOUT_SECONDS = 180.0
UPDATE_LOCK_TIMEOUT_SECONDS = 20.0
UPDATE_LOCK_STALE_SECONDS = 30.0
UPDATE_LOCK_POLL_SECONDS = 0.05
UPDATE_MAX_JSON_BYTES = 64 * 1024
UPDATE_HANDOFF_GRACE_SECONDS = 15.0

_LOCAL_HOST = socket.gethostname().strip().lower() or "unknown"


class UpdateInProgressError(RuntimeError):
    """Raised when task submission races with an installation update."""


class UpdateCoordinationError(RuntimeError):
    """Raised when the shared update state cannot be read or changed safely."""


def coordination_state_dir(path: str | os.PathLike[str] | None = None) -> str:
    """Return the persistent state directory beside the installed package."""

    selected = path if path is not None else os.getenv(UPDATE_STATE_DIR_ENV)
    if selected:
        return os.path.abspath(os.path.expanduser(os.path.expandvars(os.fspath(selected))))
    package_parent = Path(__file__).resolve().parent.parent
    return str(package_parent / ".pyruns-update")


def _bounded_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(UPDATE_MAX_JSON_BYTES + 1)
    except (FileNotFoundError, OSError):
        return None
    if len(raw) > UPDATE_MAX_JSON_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(encoded) > UPDATE_MAX_JSON_BYTES:
        raise ValueError(f"Update state exceeds {UPDATE_MAX_JSON_BYTES} bytes")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
    finally:
        if temporary:
            try:
                os.remove(temporary)
            except OSError:
                pass


def process_record(*, record_id: str, kind: str) -> dict[str, Any]:
    return {
        "id": str(record_id),
        "kind": str(kind),
        "host": _LOCAL_HOST,
        "pid": os.getpid(),
        "process_create_time": get_process_create_time(os.getpid()),
        "heartbeat_at": time.time(),
    }


def _record_owner_is_alive(record: dict[str, Any]) -> bool | None:
    if str(record.get("host", "") or "").lower() != _LOCAL_HOST:
        return None
    try:
        pid = int(record.get("pid", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if pid <= 0 or not is_pid_running(pid):
        return False
    expected = record.get("process_create_time")
    if expected is None:
        return True
    try:
        expected_value = float(expected)
    except (TypeError, ValueError, OverflowError):
        return True
    actual = get_process_create_time(pid)
    return actual is None or abs(actual - expected_value) <= 0.01


def _record_is_live(record: dict[str, Any], *, timeout: float) -> bool:
    owner_alive = _record_owner_is_alive(record)
    try:
        heartbeat = float(record.get("heartbeat_at", 0.0) or 0.0)
    except (TypeError, ValueError, OverflowError):
        return False
    if owner_alive is False:
        return (
            str(record.get("phase", "") or "") == "handoff"
            and heartbeat > 0
            and time.time() - heartbeat <= UPDATE_HANDOFF_GRACE_SECONDS
        )
    return heartbeat > 0 and time.time() - heartbeat <= max(1.0, timeout)


class CoordinationStore:
    """Atomic JSON state shared by processes using one Pyruns installation."""

    def __init__(self, state_dir: str | os.PathLike[str] | None = None) -> None:
        self.state_dir = coordination_state_dir(state_dir)
        self.request_path = os.path.join(self.state_dir, UPDATE_REQUEST_FILENAME)
        self.lock_path = os.path.join(self.state_dir, UPDATE_LOCK_FILENAME)
        self.instances_dir = os.path.join(self.state_dir, UPDATE_INSTANCES_DIRNAME)
        self.activities_dir = os.path.join(self.state_dir, UPDATE_ACTIVITIES_DIRNAME)

    def ensure(self) -> None:
        os.makedirs(self.instances_dir, exist_ok=True)
        os.makedirs(self.activities_dir, exist_ok=True)

    @staticmethod
    def _lock_snapshot(path: str) -> tuple[tuple[int, int, int, int], bytes] | None:
        try:
            with open(path, "rb") as handle:
                stat = os.fstat(handle.fileno())
                content = handle.read(4096)
        except OSError:
            return None
        return (
            (int(stat.st_dev), int(stat.st_ino), int(stat.st_mtime_ns), int(stat.st_size)),
            content,
        )

    @staticmethod
    def _quarantine_lock(
        path: str,
        expected: tuple[tuple[int, int, int, int], bytes],
    ) -> bool:
        if CoordinationStore._lock_snapshot(path) != expected:
            return False
        quarantine = f"{path}.stale-{os.getpid()}-{secrets.token_hex(8)}"
        try:
            os.replace(path, quarantine)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if CoordinationStore._lock_snapshot(quarantine) != expected:
            try:
                if not os.path.exists(path):
                    os.replace(quarantine, path)
            except OSError:
                pass
            return False
        try:
            os.remove(quarantine)
        except OSError:
            return False
        return True

    @staticmethod
    def _lock_is_stale(snapshot: tuple[tuple[int, int, int, int], bytes]) -> bool:
        try:
            owner = json.loads(snapshot[1].decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            owner = {}
        if isinstance(owner, dict):
            owner_alive = _record_owner_is_alive(owner)
            if owner_alive is not None:
                return not owner_alive
        age = max(0.0, time.time() - snapshot[0][2] / 1_000_000_000)
        return age >= UPDATE_LOCK_STALE_SECONDS

    @contextmanager
    def locked(self, *, timeout: float = UPDATE_LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
        """Acquire the short-lived cross-process state mutex."""

        self.ensure()
        deadline = time.monotonic() + max(0.0, float(timeout))
        owner = process_record(record_id=secrets.token_hex(16), kind="lock")
        owner["acquired_at"] = time.time()
        encoded = json.dumps(owner, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_CLOEXEC", 0))
        while True:
            try:
                fd = os.open(self.lock_path, flags, 0o600)
            except FileExistsError as exc:
                snapshot = self._lock_snapshot(self.lock_path)
                if snapshot is not None and self._lock_is_stale(snapshot):
                    if self._quarantine_lock(self.lock_path, snapshot):
                        continue
                if time.monotonic() >= deadline:
                    raise UpdateCoordinationError(
                        "Timed out waiting for the shared Pyruns update lock."
                    ) from exc
                time.sleep(UPDATE_LOCK_POLL_SECONDS)
                continue
            try:
                offset = 0
                while offset < len(encoded):
                    written = os.write(fd, encoded[offset:])
                    if written <= 0:
                        raise OSError("Could not write update lock owner")
                    offset += written
                os.fsync(fd)
            except BaseException:
                try:
                    os.remove(self.lock_path)
                except OSError:
                    pass
                raise
            finally:
                os.close(fd)
            break
        try:
            yield
        finally:
            snapshot = self._lock_snapshot(self.lock_path)
            if snapshot is not None and snapshot[1] == encoded:
                self._quarantine_lock(self.lock_path, snapshot)

    def read_request_locked(self) -> dict[str, Any] | None:
        return _bounded_json(self.request_path)

    @staticmethod
    def request_is_active(request: dict[str, Any] | None) -> bool:
        return bool(request and str(request.get("stage", "")) in UPDATE_ACTIVE_STAGES)

    @staticmethod
    def request_is_stale(request: dict[str, Any]) -> bool:
        if not CoordinationStore.request_is_active(request):
            return False
        owner_alive = _record_owner_is_alive(request)
        try:
            heartbeat = float(request.get("heartbeat_at", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            return True
        if owner_alive is False:
            return heartbeat <= 0 or time.time() - heartbeat > UPDATE_HANDOFF_GRACE_SECONDS
        return heartbeat <= 0 or time.time() - heartbeat > UPDATE_REQUEST_TIMEOUT_SECONDS

    def recover_stale_request_locked(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        result = {
            "ok": False,
            "previous_version": str(request.get("previous_version", "") or ""),
            "installed_version": str(request.get("previous_version", "") or ""),
            "exit_code": 1,
        }
        recovered = dict(request)
        recovered.update(
            {
                "stage": "completed",
                "heartbeat_at": time.time(),
                "completed_at": time.time(),
                "result": result,
                "error": "The update owner exited before coordination completed.",
            }
        )
        _atomic_write_json(self.request_path, recovered)
        return recovered

    def active_request_locked(self, *, recover: bool = True) -> dict[str, Any] | None:
        request = self.read_request_locked()
        if not self.request_is_active(request):
            return None
        assert request is not None
        if self.request_is_stale(request):
            if recover:
                self.recover_stale_request_locked(request)
            return None
        return request

    def read_request(self) -> dict[str, Any] | None:
        with self.locked():
            self.active_request_locked(recover=True)
            return self.read_request_locked()

    def write_request_locked(self, request: dict[str, Any]) -> None:
        _atomic_write_json(self.request_path, request)

    def update_request_locked(self, request_id: str, **changes: Any) -> dict[str, Any] | None:
        request = self.read_request_locked()
        if request is None or str(request.get("request_id", "")) != str(request_id):
            return None
        updated = dict(request)
        updated.update(changes)
        _atomic_write_json(self.request_path, updated)
        return updated

    def record_path(self, group: str, record_id: str) -> str:
        if not record_id or any(char not in "0123456789abcdef" for char in record_id.lower()):
            raise ValueError("Invalid update coordination record id")
        directory = self.instances_dir if group == "instances" else self.activities_dir
        return os.path.join(directory, f"{record_id}.json")

    def write_record(self, group: str, record_id: str, payload: dict[str, Any]) -> None:
        self.ensure()
        _atomic_write_json(self.record_path(group, record_id), payload)

    def remove_record(self, group: str, record_id: str) -> None:
        try:
            os.remove(self.record_path(group, record_id))
        except FileNotFoundError:
            pass

    def live_records_locked(self, group: str) -> list[dict[str, Any]]:
        directory = self.instances_dir if group == "instances" else self.activities_dir
        try:
            names = os.listdir(directory)
        except OSError:
            return []
        live: list[dict[str, Any]] = []
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            record = _bounded_json(path)
            if record is not None and _record_is_live(
                record,
                timeout=UPDATE_LEASE_TIMEOUT_SECONDS,
            ):
                live.append(record)
                continue
            try:
                os.remove(path)
            except OSError:
                pass
        return live


class EnvironmentActivityLease:
    """Keep an updater away while a CLI submission or runner is active."""

    def __init__(
        self,
        kind: str,
        *,
        state_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.store = CoordinationStore(state_dir)
        self.activity_id = secrets.token_hex(16)
        self.kind = str(kind)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._disabled = False

    def _payload(self) -> dict[str, Any]:
        return process_record(record_id=self.activity_id, kind=self.kind)

    def start(self) -> "EnvironmentActivityLease":
        if self._started:
            return self
        if self._disabled:
            return self
        try:
            with self.store.locked():
                if self.store.active_request_locked(recover=True) is not None:
                    raise UpdateInProgressError(
                        "Pyruns is updating; new tasks are disabled until every UI restarts."
                    )
                self.store.write_record("activities", self.activity_id, self._payload())
        except PermissionError:
            # A read-only installation cannot run the in-place updater either.
            # Preserve ordinary CLI execution unless a readable live gate exists.
            request = self.store.read_request_locked()
            if (
                self.store.request_is_active(request)
                and request is not None
                and not self.store.request_is_stale(request)
            ):
                raise UpdateInProgressError(
                    "Pyruns is updating; new tasks are disabled until every UI restarts."
                )
            self._disabled = True
            return self
        self._started = True
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="pyruns-activity-heartbeat",
            daemon=True,
        )
        try:
            self._thread.start()
        except BaseException:
            self.store.remove_record("activities", self.activity_id)
            self._started = False
            raise
        return self

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(UPDATE_HEARTBEAT_SECONDS):
            try:
                self.store.write_record("activities", self.activity_id, self._payload())
            except OSError:
                pass

    def close(self) -> None:
        if self._disabled:
            return
        if not self._started:
            return
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self.store.remove_record("activities", self.activity_id)
        self._started = False

    def __enter__(self) -> "EnvironmentActivityLease":
        return self.start()

    def __exit__(self, *_args: Any) -> None:
        self.close()
