from __future__ import annotations

from unittest.mock import Mock

from pyruns.web.session_recovery import SessionRecovery


def test_session_recovery_rotates_one_previous_token(tmp_path):
    path = tmp_path / "session.json"
    first = SessionRecovery(str(path), scope="workspace-a", token="first")
    second = SessionRecovery(str(path), scope="workspace-a", token="second")

    assert first.accepts("first")
    assert not first.accepts("second")
    assert second.accepts("second")
    assert second.accepts("first")
    assert not second.accepts("forged")


def test_session_recovery_does_not_cross_workspace_scopes(tmp_path):
    path = tmp_path / "session.json"
    SessionRecovery(str(path), scope="workspace-a", token="first")
    second = SessionRecovery(str(path), scope="workspace-b", token="second")

    assert second.accepts("second")
    assert not second.accepts("first")


def test_session_recovery_previous_token_expires(monkeypatch, tmp_path):
    path = tmp_path / "session.json"
    clock = Mock()
    clock.time.side_effect = (100.0, 100.0, 401.0)
    monkeypatch.setattr("pyruns.web.session_recovery.time", clock)
    SessionRecovery(str(path), scope="workspace", token="first")
    second = SessionRecovery(str(path), scope="workspace", token="second")

    assert second.accepts("first") is False


def test_session_recovery_ignores_an_old_state_record(monkeypatch, tmp_path):
    path = tmp_path / "session.json"
    clock = Mock()
    clock.time.side_effect = (100.0, 401.0)
    monkeypatch.setattr("pyruns.web.session_recovery.time", clock)
    SessionRecovery(str(path), scope="workspace", token="first")
    second = SessionRecovery(str(path), scope="workspace", token="second")

    assert second.accepts("first") is False


def test_session_recovery_disables_itself_when_state_storage_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "pyruns.web.session_recovery._atomic_write",
        Mock(side_effect=PermissionError("read-only")),
    )

    recovery = SessionRecovery(str(tmp_path / "session.json"), scope="workspace", token="token")

    assert recovery.available is False
    assert recovery.accepts("token") is False


def test_session_recovery_ignores_an_invalid_state_path(tmp_path):
    invalid_path = str(tmp_path / "state") + "\x00invalid"

    recovery = SessionRecovery(invalid_path, scope="workspace", token="token")

    assert recovery.available is False
    assert recovery.accepts("token") is False
