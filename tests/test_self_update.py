import json
import os
import sys
from types import SimpleNamespace

import pytest

import pyruns.update_coordination as update_coordination
from pyruns.update_coordination import (
    CoordinationStore,
    EnvironmentActivityLease,
    UpdateInProgressError,
    process_record,
)
from pyruns.web import self_update


class _Runtime:
    def __init__(self, active_count: int) -> None:
        self.active_count = active_count

    def active_task_count(self) -> int:
        return self.active_count


class _PyPIResponse:
    def __init__(self, payload: bytes, *, content_length: int | None = None) -> None:
        self.payload = payload
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]


def test_update_coordinator_requires_idle_runtime_and_gates_new_starts():
    shutdowns = []
    active = self_update.UiUpdateCoordinator(lambda: shutdowns.append("active"))

    with pytest.raises(self_update.ActiveTasksError, match="2 queued or running tasks"):
        active.prepare(_Runtime(2))
    assert active.requested is False
    with active.task_start_guard():
        pass

    idle = self_update.UiUpdateCoordinator(lambda: shutdowns.append("idle"))
    idle.prepare(_Runtime(0))

    assert idle.requested is True
    assert idle.state == "restarting"
    with pytest.raises(self_update.UpdateInProgressError, match="new tasks are disabled"):
        with idle.task_start_guard():
            pass
    idle.trigger_shutdown()
    assert shutdowns == ["idle"]


def test_update_coordinator_fails_closed_when_idle_check_fails():
    class BrokenRuntime:
        def active_task_count(self):
            raise OSError("disk unavailable")

    coordinator = self_update.UiUpdateCoordinator(lambda: None)

    with pytest.raises(self_update.UpdateCheckError, match="Could not verify"):
        coordinator.prepare(BrokenRuntime())
    assert coordinator.requested is False


def test_shared_coordinators_gate_every_ui_and_restart_followers(tmp_path):
    shutdowns: list[str] = []
    state_dir = tmp_path / "coordination"
    owner = self_update.UiUpdateCoordinator(
        lambda: shutdowns.append("owner"),
        state_dir=str(state_dir),
        shared=True,
        current_version="0.3.0",
    )
    follower = self_update.UiUpdateCoordinator(
        lambda: shutdowns.append("follower"),
        state_dir=str(state_dir),
        shared=True,
        current_version="0.3.0",
    )
    owner.attach(_Runtime(0))
    follower.attach(_Runtime(0))
    try:
        assert owner.prepare(_Runtime(0)) is True
        with pytest.raises(UpdateInProgressError, match="every UI restarts"):
            with follower.task_start_guard():
                pass

        follower._monitor_tick()

        assert follower.requested is True
        assert shutdowns == ["follower"]
        follower_handoff = follower.handoff()
        assert follower_handoff["role"] == "follower"
        follower_record = CoordinationStore(state_dir).live_records_locked("instances")
        assert any(
            item.get("id") == follower.instance_id and item.get("phase") == "handoff"
            for item in follower_record
        )
    finally:
        follower.close()
        owner.close(failed_handoff=True)


def test_shared_update_refuses_remote_ui_and_cli_activity(tmp_path):
    state_dir = tmp_path / "coordination"
    owner = self_update.UiUpdateCoordinator(
        lambda: None,
        state_dir=str(state_dir),
        shared=True,
        current_version="0.3.0",
    )
    remote = self_update.UiUpdateCoordinator(
        lambda: None,
        state_dir=str(state_dir),
        shared=True,
        current_version="0.3.0",
    )
    owner.attach(_Runtime(0))
    remote.attach(_Runtime(2))
    try:
        with pytest.raises(self_update.ActiveTasksError, match="2 queued or running tasks"):
            owner.prepare(_Runtime(0))

        remote.close()
        with EnvironmentActivityLease("cli-runner", state_dir=str(state_dir)):
            with pytest.raises(self_update.ActiveTasksError, match="queued or running task"):
                owner.prepare(_Runtime(0))
    finally:
        remote.close()
        owner.close(failed_handoff=True)


def test_shared_task_guard_preserves_task_start_errors(tmp_path):
    coordinator = self_update.UiUpdateCoordinator(
        lambda: None,
        state_dir=str(tmp_path / "coordination"),
        shared=True,
        current_version="0.3.0",
    )
    coordinator.attach(_Runtime(0))
    try:
        with pytest.raises(ValueError, match="task start failed"):
            with coordinator.task_start_guard():
                raise ValueError("task start failed")
    finally:
        coordinator.close()


def test_coordinated_update_waits_for_handoff_then_publishes_to_waiter(tmp_path, monkeypatch):
    state_dir = tmp_path / "coordination"
    store = CoordinationStore(state_dir)
    owner_id = "a" * 32
    follower_id = "b" * 32
    request_id = "c" * 32
    request = process_record(record_id=owner_id, kind="ui-update")
    request.update(
        {
            "request_id": request_id,
            "owner_instance_id": owner_id,
            "operation": "upgrade",
            "stage": "draining",
            "previous_version": "0.3.0",
        }
    )
    store.ensure()
    with store.locked():
        store.write_request_locked(request)
    follower = process_record(record_id=follower_id, kind="ui")
    follower.update(
        {
            "phase": "handoff",
            "active_count": 0,
            "request_id": request_id,
            "version": "0.3.0",
        }
    )
    store.write_record("instances", follower_id, follower)
    sleeps: list[float] = []

    def release_follower(seconds: float) -> None:
        sleeps.append(seconds)
        follower["phase"] = "waiting"
        store.write_record("instances", follower_id, follower)

    result = {
        "ok": True,
        "previous_version": "0.3.0",
        "installed_version": "0.4.0",
        "exit_code": 0,
    }
    monkeypatch.setattr(self_update.time, "sleep", release_follower)
    monkeypatch.setattr(self_update, "run_pip_upgrade", lambda _version: dict(result))

    assert self_update.run_coordinated_update(
        state_dir=str(state_dir),
        request_id=request_id,
        instance_id=owner_id,
        previous_version="0.3.0",
    ) == result
    assert sleeps
    assert store.read_request()["stage"] == "completed"
    assert self_update.wait_for_coordinated_update(
        state_dir=str(state_dir),
        request_id=request_id,
        instance_id=follower_id,
        previous_version="0.3.0",
    ) == result
    with store.locked():
        assert store.live_records_locked("instances") == []


def test_activity_lease_rejects_starts_while_shared_gate_is_active(tmp_path):
    state_dir = tmp_path / "coordination"
    store = CoordinationStore(state_dir)
    owner_id = "d" * 32
    request = process_record(record_id=owner_id, kind="ui-update")
    request.update(
        {
            "request_id": "e" * 32,
            "owner_instance_id": owner_id,
            "stage": "updating",
            "previous_version": "0.3.0",
        }
    )
    store.ensure()
    with store.locked():
        store.write_request_locked(request)

    with pytest.raises(UpdateInProgressError, match="new tasks are disabled"):
        EnvironmentActivityLease("cli-runner", state_dir=str(state_dir)).start()


def test_remote_nfs_leases_survive_brief_disconnects_and_expire(tmp_path):
    store = CoordinationStore(tmp_path / "coordination")
    instance_id = "f" * 32
    remote = process_record(record_id=instance_id, kind="ui")
    remote.update(
        {
            "host": "remote-worker",
            "phase": "serving",
            "active_count": 0,
            "request_id": "",
        }
    )
    store.write_record("instances", instance_id, remote)

    with store.locked():
        assert [item["id"] for item in store.live_records_locked("instances")] == [
            instance_id
        ]

    remote["heartbeat_at"] = (
        self_update.time.time()
        - update_coordination.UPDATE_LEASE_TIMEOUT_SECONDS
        - 1
    )
    store.write_record("instances", instance_id, remote)
    with store.locked():
        assert store.live_records_locked("instances") == []


def test_external_install_version_change_restarts_shared_ui_when_idle(tmp_path, monkeypatch):
    shutdowns: list[str] = []
    coordinator = self_update.UiUpdateCoordinator(
        lambda: shutdowns.append("shutdown"),
        state_dir=str(tmp_path / "coordination"),
        shared=True,
        current_version="0.3.0",
    )
    coordinator.attach(_Runtime(0))
    monkeypatch.setattr(coordinator, "_installed_version", lambda _fallback: "0.4.0")
    coordinator._next_version_check = 0.0
    try:
        coordinator._monitor_tick()

        assert coordinator.requested is True
        assert shutdowns == ["shutdown"]
        handoff = coordinator.handoff()
        assert handoff["operation"] == "restart"
        assert handoff["target_version"] == "0.4.0"
    finally:
        coordinator.close(failed_handoff=True)


@pytest.mark.parametrize(
    ("current_version", "latest_version", "update_available"),
    [
        ("0.3.0", "0.4.0", True),
        ("0.3.0", "0.3.0", False),
        ("0.4.0.dev1", "0.3.0", False),
    ],
)
def test_check_latest_version_uses_pypi_and_pep440(
    monkeypatch,
    current_version,
    latest_version,
    update_available,
):
    observed = {}

    def fake_urlopen(request, *, timeout):
        observed["request"] = request
        observed["timeout"] = timeout
        payload = json.dumps({"info": {"version": latest_version}}).encode("utf-8")
        return _PyPIResponse(payload, content_length=len(payload))

    monkeypatch.setattr(self_update.urllib.request, "urlopen", fake_urlopen)

    assert self_update.check_latest_version(current_version) == {
        "current_version": current_version,
        "latest_version": latest_version,
        "update_available": update_available,
    }
    assert observed["request"].full_url == self_update.PYPI_PROJECT_JSON_URL
    assert observed["request"].get_header("User-agent").startswith("pyruns/")
    assert observed["timeout"] == self_update.PYPI_CHECK_TIMEOUT_SECONDS


def test_check_latest_version_handles_offline_and_invalid_responses(monkeypatch):
    def offline(*_args, **_kwargs):
        raise self_update.urllib.error.URLError("offline")

    monkeypatch.setattr(self_update.urllib.request, "urlopen", offline)
    with pytest.raises(self_update.LatestVersionCheckError, match="check PyPI"):
        self_update.check_latest_version("0.3.0")

    monkeypatch.setattr(
        self_update.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _PyPIResponse(b'{"info": {"version": "invalid version"}}'),
    )
    with pytest.raises(self_update.LatestVersionCheckError, match="invalid"):
        self_update.check_latest_version("0.3.0")

    monkeypatch.setattr(
        self_update.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _PyPIResponse(
            b"{}",
            content_length=self_update.PYPI_MAX_RESPONSE_BYTES + 1,
        ),
    )
    with pytest.raises(self_update.LatestVersionCheckError, match="large"):
        self_update.check_latest_version("0.3.0")


def test_run_pip_upgrade_uses_current_interpreter(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if "pip" in command:
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0, stdout="0.4.0\n")

    monkeypatch.setattr(self_update.subprocess, "run", fake_run)

    result = self_update.run_pip_upgrade("0.3.0")

    assert calls[0][0] == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pyruns",
    ]
    assert calls[0][1] == {"check": False}
    assert result == {
        "ok": True,
        "previous_version": "0.3.0",
        "installed_version": "0.4.0",
        "exit_code": 0,
    }


def test_run_pip_upgrade_failure_keeps_relaunch_result(monkeypatch):
    def fake_run(command, **_kwargs):
        if "pip" in command:
            raise OSError("offline")
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(self_update.subprocess, "run", fake_run)

    result = self_update.run_pip_upgrade("0.3.0")

    assert result == {
        "ok": False,
        "previous_version": "0.3.0",
        "installed_version": "0.3.0",
        "exit_code": 1,
    }


def test_process_replacements_keep_token_out_of_command_line(monkeypatch):
    executions = []

    def fake_execve(executable, command, environment):
        executions.append((executable, command, environment))

    monkeypatch.setattr(self_update.os, "execve", fake_execve)
    monkeypatch.setattr(self_update.sys, "executable", "python-test")

    self_update.replace_process_with_updater(
        port=8123,
        token="private-token",
        previous_version="0.3.0",
    )
    result = {
        "ok": True,
        "previous_version": "0.3.0",
        "installed_version": "0.4.0",
        "exit_code": 0,
    }
    self_update.relaunch_ui(port=8123, token="private-token", result=result)

    updater_exec = executions[0]
    assert updater_exec[0] == "python-test"
    assert updater_exec[1] == [
        "python-test",
        "-m",
        "pyruns.web.self_update",
        "--port",
        "8123",
        "--previous-version",
        "0.3.0",
    ]
    assert "private-token" not in updater_exec[1]
    assert updater_exec[2][self_update.UI_TOKEN_ENV] == "private-token"

    server_exec = executions[1]
    assert server_exec[1] == [
        "python-test",
        "-m",
        "pyruns.web.app",
        "--port",
        "8123",
        "--no-browser",
    ]
    assert server_exec[2][self_update.UI_PRODUCTION_RESTART_ENV] == "1"
    assert server_exec[2][self_update.UI_TOKEN_ENV] == "private-token"
    assert json.loads(server_exec[2][self_update.UI_UPDATE_RESULT_ENV]) == result


def test_updater_main_removes_token_before_pip_and_relaunches(monkeypatch):
    observed = {}
    monkeypatch.setenv(self_update.UI_TOKEN_ENV, "private-token")

    def fake_upgrade(previous_version):
        observed["pip_token"] = os.environ.get(self_update.UI_TOKEN_ENV)
        observed["previous_version"] = previous_version
        return {"ok": True, "previous_version": previous_version, "installed_version": "0.4.0", "exit_code": 0}

    def fake_relaunch(**kwargs):
        observed["relaunch"] = kwargs

    monkeypatch.setattr(self_update, "run_pip_upgrade", fake_upgrade)
    monkeypatch.setattr(self_update, "relaunch_ui", fake_relaunch)

    assert self_update.main(["--port", "8123", "--previous-version", "0.3.0"]) == 1
    assert observed["pip_token"] is None
    assert observed["previous_version"] == "0.3.0"
    assert observed["relaunch"]["port"] == 8123
    assert observed["relaunch"]["token"] == "private-token"


def test_waiter_main_uses_shared_result_without_running_upgrade(monkeypatch):
    observed = {}
    result = {
        "ok": True,
        "previous_version": "0.3.0",
        "installed_version": "0.4.0",
        "exit_code": 0,
    }
    monkeypatch.setenv(self_update.UI_TOKEN_ENV, "private-token")

    def fake_wait(**kwargs):
        observed["wait"] = kwargs
        return dict(result)

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("a follower must not run the shared upgrade")

    monkeypatch.setattr(self_update, "wait_for_coordinated_update", fake_wait)
    monkeypatch.setattr(self_update, "run_coordinated_update", unexpected_call)
    monkeypatch.setattr(self_update, "run_pip_upgrade", unexpected_call)
    monkeypatch.setattr(
        self_update,
        "relaunch_ui",
        lambda **kwargs: observed.setdefault("relaunch", kwargs),
    )

    assert self_update.main(
        [
            "--wait",
            "--port",
            "8123",
            "--previous-version",
            "0.3.0",
            "--request-id",
            "request-id",
            "--instance-id",
            "instance-id",
            "--state-dir",
            "state-dir",
        ]
    ) == 1
    assert observed["wait"] == {
        "state_dir": "state-dir",
        "request_id": "request-id",
        "instance_id": "instance-id",
        "previous_version": "0.3.0",
    }
    assert observed["relaunch"] == {
        "port": 8123,
        "token": "private-token",
        "result": result,
    }


def test_incomplete_waiter_arguments_never_run_upgrade(monkeypatch):
    observed = {}
    monkeypatch.setenv(self_update.UI_TOKEN_ENV, "private-token")

    def unexpected_upgrade(*_args, **_kwargs):
        raise AssertionError("an incomplete waiter must not run pip")

    monkeypatch.setattr(self_update, "run_pip_upgrade", unexpected_upgrade)
    monkeypatch.setattr(
        self_update,
        "relaunch_ui",
        lambda **kwargs: observed.setdefault("relaunch", kwargs),
    )

    assert self_update.main(
        [
            "--wait",
            "--port",
            "8123",
            "--previous-version",
            "0.3.0",
        ]
    ) == 1
    assert observed["relaunch"]["result"] == {
        "ok": False,
        "previous_version": "0.3.0",
        "installed_version": "0.3.0",
        "exit_code": 1,
    }


def test_read_update_result_rejects_invalid_payload(monkeypatch):
    monkeypatch.setenv(self_update.UI_UPDATE_RESULT_ENV, "not-json")
    assert self_update.read_update_result() is None

    monkeypatch.setenv(
        self_update.UI_UPDATE_RESULT_ENV,
        json.dumps({"ok": False, "exit_code": "not-an-integer"}),
    )
    assert self_update.read_update_result() is None

    monkeypatch.setenv(
        self_update.UI_UPDATE_RESULT_ENV,
        json.dumps({"ok": False, "previous_version": "0.3.0", "installed_version": "0.3.0", "exit_code": 1}),
    )
    assert self_update.read_update_result() == {
        "ok": False,
        "previous_version": "0.3.0",
        "installed_version": "0.3.0",
        "exit_code": 1,
    }
