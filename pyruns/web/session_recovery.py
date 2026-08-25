"""Bounded local session handoff for UI process restarts."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import socket
import tempfile
import time
from pathlib import Path
from typing import Any


SESSION_STATE_ENV = "PYRUNS_UI_SESSION_STATE"
SESSION_RECOVERY_GRACE_SECONDS = 300.0
SESSION_STATE_MAX_BYTES = 8 * 1024
SESSION_STATE_SCHEMA = 1


def _token_digest(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _scope_digest(scope: str) -> str:
    return hashlib.sha256(str(scope).encode("utf-8")).hexdigest()


def session_scope(*, cookie_nonce: str, workspace: str = "") -> str:
    """Return the host/port/workspace scope bound into one recovery record."""

    host = socket.gethostname().strip().lower() or "unknown"
    workspace_path = os.path.normcase(os.path.abspath(str(workspace or "")))
    return f"{host}\n{str(cookie_nonce)}\n{workspace_path}"


def default_session_state_path(*, port: int, workspace: str = "") -> str:
    """Return a user-local, port-scoped state path for one UI session slot."""

    try:
        user_root = os.path.normcase(os.path.abspath(str(Path.home())))
    except (OSError, RuntimeError):
        user_root = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    host = socket.gethostname().strip().lower() or "unknown"
    cookie_nonce = f"{int(port):032x}"
    workspace_path = os.path.normcase(os.path.abspath(str(workspace or "")))
    scope = f"{user_root}\n{host}\n{cookie_nonce}\n{workspace_path}"
    root = Path(tempfile.gettempdir()) / "pyruns-session"
    return str(root / f"{_scope_digest(scope)[:32]}.json")


def _read_state(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(SESSION_STATE_MAX_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(raw) > SESSION_STATE_MAX_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != SESSION_STATE_SCHEMA:
        return None
    return payload


def _valid_digest(value: Any) -> str:
    text = str(value or "").lower()
    return text if len(text) == 64 and all(char in "0123456789abcdef" for char in text) else ""


def _valid_timestamp(value: Any) -> float | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return timestamp if math.isfinite(timestamp) else None


def _atomic_write(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    if len(encoded) > SESSION_STATE_MAX_BYTES:
        raise ValueError("UI session state is unexpectedly large")
    fd = -1
    temporary = ""
    for _ in range(4):
        candidate = os.path.join(directory, f".pyruns-session-{secrets.token_hex(8)}.tmp")
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        temporary = candidate
        break
    if fd < 0 or not temporary:
        raise OSError("could not allocate a temporary UI session file")
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(encoded)
            handle.flush()
        os.replace(temporary, path)
        temporary = ""
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary:
            try:
                os.remove(temporary)
            except OSError:
                pass


class SessionRecovery:
    """Validate one previous session during a short, restart-only grace period."""

    def __init__(self, path: str, *, scope: str, token: str) -> None:
        self.path = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path))))
        self.scope_digest = _scope_digest(scope)
        self.active_digest = _token_digest(token)
        self.previous_digest = ""
        self.previous_expires_at = 0.0
        self.available = False
        self._register()

    def _register(self) -> None:
        now = time.time()
        previous = _read_state(self.path)
        if previous is not None and str(previous.get("scope", "")) == self.scope_digest:
            old_active = _valid_digest(previous.get("active"))
            previous_updated_at = _valid_timestamp(previous.get("updated_at"))
            previous_age = (
                now - previous_updated_at
                if previous_updated_at is not None
                else SESSION_RECOVERY_GRACE_SECONDS + 1
            )
            if old_active and 0 <= previous_age <= SESSION_RECOVERY_GRACE_SECONDS:
                self.previous_digest = old_active
                self.previous_expires_at = previous_updated_at + SESSION_RECOVERY_GRACE_SECONDS
        payload = {
            "schema": SESSION_STATE_SCHEMA,
            "scope": self.scope_digest,
            "active": self.active_digest,
            "updated_at": now,
        }
        try:
            _atomic_write(self.path, payload)
        except (OSError, ValueError):
            return
        self.available = True

    def accepts(self, token: str) -> bool:
        """Return whether ``token`` belongs to this or the immediately prior UI."""

        if not self.available:
            return False
        candidate = _token_digest(token)
        if hmac.compare_digest(candidate, self.active_digest):
            return True
        return self.accepts_previous(token, candidate=candidate)

    def accepts_previous(self, token: str, *, candidate: str | None = None) -> bool:
        """Return whether ``token`` belongs to the immediately prior UI only."""

        if not self.available or not self.previous_digest or time.time() > self.previous_expires_at:
            return False
        candidate = candidate or _token_digest(token)
        return bool(
            hmac.compare_digest(candidate, self.previous_digest)
        )
