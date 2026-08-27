"""Persistent local browser sessions across UI process restarts."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import socket
import tempfile
from pathlib import Path
from typing import Any


SESSION_STATE_ENV = "PYRUNS_UI_SESSION_STATE"
SESSION_SCOPE_ENV = "PYRUNS_UI_SESSION_SCOPE"
SESSION_STATE_DIR_ENV = "PYRUNS_UI_SESSION_STATE_DIR"
SESSION_COOKIE_MAX_AGE_SECONDS = 400 * 24 * 60 * 60
SESSION_STATE_MAX_BYTES = 8 * 1024
SESSION_STATE_SCHEMA = 2
_LEGACY_SESSION_STATE_SCHEMA = 1


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
    """Return a persistent user-local state path, migrating the old temp record."""

    try:
        user_root = os.path.normcase(os.path.abspath(str(Path.home())))
    except (OSError, RuntimeError):
        user_root = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    host = socket.gethostname().strip().lower() or "unknown"
    cookie_nonce = f"{int(port):032x}"
    workspace_path = os.path.normcase(os.path.abspath(str(workspace or "")))
    scope = f"{user_root}\n{host}\n{cookie_nonce}\n{workspace_path}"
    filename = f"{_scope_digest(scope)[:32]}.json"
    configured_root = str(os.getenv(SESSION_STATE_DIR_ENV, "") or "").strip()
    persistent_root = (
        Path(os.path.abspath(os.path.expanduser(os.path.expandvars(configured_root))))
        if configured_root
        else Path(user_root) / ".pyruns" / "sessions"
    )
    persistent_path = persistent_root / filename
    legacy_path = Path(tempfile.gettempdir()) / "pyruns-session" / filename
    if persistent_path == legacy_path or persistent_path.exists():
        return str(persistent_path)

    legacy = _read_state(str(legacy_path))
    if legacy is None:
        return str(persistent_path)
    try:
        _atomic_write(str(persistent_path), legacy)
    except (OSError, ValueError):
        return str(legacy_path)
    return str(persistent_path)


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
    if (
        not isinstance(payload, dict)
        or payload.get("schema") not in {_LEGACY_SESSION_STATE_SCHEMA, SESSION_STATE_SCHEMA}
    ):
        return None
    return payload


def _valid_digest(value: Any) -> str:
    text = str(value or "").lower()
    return text if len(text) == 64 and all(char in "0123456789abcdef" for char in text) else ""


def _valid_session_token(value: Any) -> str:
    text = str(value or "")
    if not 32 <= len(text) <= 128:
        return ""
    valid = all(
        char.isascii() and (char.isalnum() or char in "-_")
        for char in text
    )
    return text if valid else ""


def _valid_digest_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        digest = _valid_digest(item)
        if digest and digest not in result:
            result.append(digest)
    return result


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
            os.fsync(handle.fileno())
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
    """Issue and validate one durable local browser session credential."""

    def __init__(self, path: str, *, scope: str, token: str) -> None:
        self.path = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path))))
        self.scope_digest = _scope_digest(scope)
        self.active_digest = _token_digest(token)
        self.cookie_token = ""
        self._accepted_digests: tuple[str, ...] = ()
        self.available = False
        self._register()

    def _register(self) -> None:
        previous = _read_state(self.path)
        cookie_token = ""
        legacy_digests: list[str] = []
        if previous is not None and str(previous.get("scope", "")) == self.scope_digest:
            cookie_token = _valid_session_token(previous.get("session_token"))
            legacy_digests = _valid_digest_list(previous.get("legacy_digests"))
            if previous.get("schema") == _LEGACY_SESSION_STATE_SCHEMA:
                old_active = _valid_digest(previous.get("active"))
                if old_active and old_active not in legacy_digests:
                    legacy_digests.append(old_active)
        if not cookie_token:
            cookie_token = secrets.token_urlsafe(32)
        cookie_digest = _token_digest(cookie_token)
        payload = {
            "schema": SESSION_STATE_SCHEMA,
            "scope": self.scope_digest,
            "active": self.active_digest,
            "session_token": cookie_token,
            "legacy_digests": legacy_digests,
        }
        try:
            _atomic_write(self.path, payload)
        except (OSError, ValueError):
            return
        self.cookie_token = cookie_token
        self._accepted_digests = tuple(
            dict.fromkeys((self.active_digest, cookie_digest, *legacy_digests))
        )
        self.available = True

    def accepts(self, token: str) -> bool:
        """Return whether ``token`` is the current or persistent local session."""

        if not self.available:
            return False
        candidate = _token_digest(token)
        return any(
            hmac.compare_digest(candidate, expected)
            for expected in self._accepted_digests
        )
