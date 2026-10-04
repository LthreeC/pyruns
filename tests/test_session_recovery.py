from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from pyruns.web import session_recovery
from pyruns.web.session_recovery import SessionRecovery


def test_session_recovery_reuses_one_persistent_cookie_across_restarts(tmp_path):
    path = tmp_path / "session.json"
    first = SessionRecovery(str(path), scope="workspace-a", token="first")
    session_token = first.cookie_token
    second = SessionRecovery(str(path), scope="workspace-a", token="second")
    third = SessionRecovery(str(path), scope="workspace-a", token="third")

    assert first.accepts("first")
    assert not first.accepts("second")
    assert second.accepts("second")
    assert second.accepts(session_token)
    assert third.accepts(session_token)
    assert second.cookie_token == session_token
    assert third.cookie_token == session_token
    assert not second.accepts("first")
    assert not second.accepts("forged")


def test_session_recovery_does_not_cross_workspace_scopes(tmp_path):
    path = tmp_path / "session.json"
    SessionRecovery(str(path), scope="workspace-a", token="first")
    second = SessionRecovery(str(path), scope="workspace-b", token="second")

    assert second.accepts("second")
    assert not second.accepts("first")


def test_session_recovery_migrates_an_old_token_without_a_time_limit(tmp_path):
    path = tmp_path / "session.json"
    old_token = "old-browser-token"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "scope": session_recovery._scope_digest("workspace"),
                "active": session_recovery._token_digest(old_token),
                "updated_at": 1.0,
            }
        ),
        encoding="ascii",
    )

    recovery = SessionRecovery(str(path), scope="workspace", token="current")
    restarted = SessionRecovery(str(path), scope="workspace", token="next")

    assert recovery.accepts(old_token)
    assert restarted.accepts(old_token)
    assert restarted.cookie_token == recovery.cookie_token


def test_default_session_state_path_migrates_the_legacy_temp_record(monkeypatch, tmp_path):
    home = tmp_path / "home"
    temp_root = tmp_path / "temp"
    monkeypatch.setattr(session_recovery.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_root))
    monkeypatch.delenv(session_recovery.SESSION_STATE_DIR_ENV, raising=False)

    target = Path(
        session_recovery.default_session_state_path(
            port=8123,
            workspace=str(tmp_path / "workspace"),
        )
    )
    legacy = temp_root / "pyruns-session" / target.name
    legacy_session = SessionRecovery(str(legacy), scope="scope", token="bootstrap")

    migrated = Path(
        session_recovery.default_session_state_path(
            port=8123,
            workspace=str(tmp_path / "workspace"),
        )
    )
    restored = SessionRecovery(str(migrated), scope="scope", token="next")

    assert migrated == target
    assert migrated.is_file()
    assert migrated.parent == home / ".pyruns" / "sessions"
    assert restored.cookie_token == legacy_session.cookie_token


def test_default_session_state_path_honors_an_isolated_state_directory(monkeypatch, tmp_path):
    state_root = tmp_path / "isolated-sessions"
    monkeypatch.setenv(session_recovery.SESSION_STATE_DIR_ENV, str(state_root))

    path = Path(
        session_recovery.default_session_state_path(
            port=8123,
            workspace=str(tmp_path / "workspace"),
        )
    )

    assert path.parent == state_root
    assert path.suffix == ".json"


def test_session_recovery_disables_itself_when_state_storage_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "pyruns.web.session_recovery._atomic_write",
        Mock(side_effect=PermissionError("read-only")),
    )

    recovery = SessionRecovery(str(tmp_path / "session.json"), scope="workspace", token="token")

    assert recovery.available is False
    assert recovery.cookie_token == ""
    assert recovery.accepts("token") is False


def test_session_recovery_ignores_an_invalid_state_path(tmp_path):
    invalid_path = str(tmp_path / "state") + "\x00invalid"

    recovery = SessionRecovery(invalid_path, scope="workspace", token="token")

    assert recovery.available is False
    assert recovery.accepts("token") is False


@pytest.mark.parametrize("schema", [[], {}, True, 1.0, 2.0, "2", None, 99])
def test_session_recovery_replaces_an_invalid_schema_without_accepting_old_tokens(tmp_path, schema):
    path = tmp_path / "session.json"
    old_cookie = "old-browser-cookie-that-must-not-be-reused"
    old_token = "old-bootstrap-token"
    path.write_text(
        json.dumps(
            {
                "schema": schema,
                "scope": session_recovery._scope_digest("workspace"),
                "active": session_recovery._token_digest(old_token),
                "session_token": old_cookie,
                "legacy_digests": [session_recovery._token_digest(old_token)],
            }
        ),
        encoding="ascii",
    )

    recovery = SessionRecovery(str(path), scope="workspace", token="current")

    assert recovery.available is True
    assert recovery.accepts("current") is True
    assert recovery.cookie_token != old_cookie
    assert recovery.accepts(old_cookie) is False
    assert recovery.accepts(old_token) is False
    assert json.loads(path.read_text(encoding="ascii"))["schema"] == session_recovery.SESSION_STATE_SCHEMA
