import errno
import http.client
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import pyruns.update_coordination as update_coordination
from pyruns import update_release
from pyruns.update_coordination import (
    CoordinationStore,
    EnvironmentActivityLease,
    UpdateCoordinationError,
    UpdateInProgressError,
    process_record,
)
from pyruns.web import self_update


class _Runtime:
    def __init__(self, active_count: int) -> None:
        self.active_count = active_count

    def active_task_count(self) -> int:
        return self.active_count


@pytest.mark.parametrize("installed", [True, False])
def test_isolated_upgrade_does_not_wait_for_active_runners(tmp_path, monkeypatch, installed):
    class DetachedRuntime(_Runtime):
        def strict_restart_active_task_count(self) -> int:
            return 0

    state_dir = str(tmp_path / "state")
    monkeypatch.setattr(update_release, "installation_root", lambda: tmp_path)
    monkeypatch.setattr(self_update.UiUpdateCoordinator, "_start_monitor", lambda self: None)

    def install(previous_version, *, target_version, install_root):
        assert (previous_version, target_version) == ("0.3.19.1", "0.3.19.2")
        assert CoordinationStore(state_dir).read_request()["stage"] == "updating"
        package = install_root / "pyruns"
        package.mkdir()
        (package / "__init__.py").touch()
        metadata = install_root / "pyruns-0.3.19.2.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text("Name: pyruns\nVersion: 0.3.19.2\n", encoding="utf-8")
        return {
            "ok": installed,
            "previous_version": previous_version,
            "installed_version": target_version if installed else previous_version,
            "exit_code": 0 if installed else 1,
        }

    monkeypatch.setattr(self_update, "run_pip_upgrade", install)
    leader = self_update.UiUpdateCoordinator(
        lambda: None, shared=True, state_dir=state_dir,
        current_version="0.3.19.1", isolated_upgrades=True,
    )
    peer = self_update.UiUpdateCoordinator(
        lambda: None, shared=True, state_dir=state_dir,
        current_version="0.3.19.1", isolated_upgrades=True,
    )
    runtime = DetachedRuntime(2)
    leader.attach(runtime)
    peer.attach(runtime)
    try:
        with EnvironmentActivityLease("cli-runner", state_dir=state_dir):
            assert leader.prepare(runtime, target_version="0.3.19.2", defer_if_busy=True)
            assert CoordinationStore(state_dir).read_request()["stage"] == "draining"
            handoff = leader.handoff()
            peer._monitor_tick()
            assert not peer.requested
            with EnvironmentActivityLease("cli-runner", state_dir=state_dir):
                result = self_update.run_coordinated_update(
                    state_dir=state_dir,
                    request_id=handoff["request_id"],
                    instance_id=handoff["instance_id"],
                    previous_version="0.3.19.1",
                    target_version="0.3.19.2",
                )
            assert result["ok"] is installed
            version, release = update_release.selected_release("0.3.19.1")
            if installed:
                assert version == "0.3.19.2" and release is not None
                assert peer.prepare(runtime, target_version="0.3.19.2")
                assert peer.handoff()["operation"] == "restart"
            else:
                assert (version, release) == ("0.3.19.1", None)
                assert not list((tmp_path / ".pyruns-releases").iterdir())
    finally:
        peer.close()
        leader.close()


def test_peer_loads_published_release_after_request_was_replaced(tmp_path, monkeypatch):
    state_dir = str(tmp_path / "state")
    store = CoordinationStore(state_dir)
    monkeypatch.setattr(self_update.UiUpdateCoordinator, "_start_monitor", lambda self: None)
    monkeypatch.setattr(self_update, "selected_release", lambda _fallback: ("0.3.21", tmp_path / "release"))

    class Observer(_Runtime):
        detached_task_runs = True

        def strict_restart_active_task_count(self) -> int:
            return 0

    shutdown = []
    peer = self_update.UiUpdateCoordinator(
        lambda: shutdown.append(True), shared=True, state_dir=state_dir,
        current_version="0.3.20", isolated_upgrades=True,
    )
    peer.attach(Observer(2))
    try:
        with store.locked():
            store.write_request_locked({
                "request_id": "later-failed",
                "stage": "completed",
                "result": {"ok": False, "installed_version": "0.3.21"},
            })
        peer._monitor_tick()
        assert shutdown == [True]
        handoff = peer.handoff()
        assert handoff["operation"] == "restart"
        assert handoff["target_version"] == "0.3.21"
        assert handoff["request_id"] == ""
    finally:
        peer.close()


@pytest.mark.parametrize("writable_coordination", [True, False])
def test_local_restart_does_not_wait_for_busy_peers(tmp_path, monkeypatch, writable_coordination):
    state_dir = str(tmp_path / "state")
    store = CoordinationStore(state_dir)
    monkeypatch.setattr(self_update.UiUpdateCoordinator, "_start_monitor", lambda self: None)
    peer = process_record(record_id="b" * 32, kind="ui")
    peer.update(phase="serving", active_count=3, restart_active_count=3)
    store.write_record("instances", peer["id"], peer)
    coordinator = self_update.UiUpdateCoordinator(
        lambda: None, shared=True, state_dir=state_dir,
        current_version="0.3.20", isolated_upgrades=True,
    )
    runtime = _Runtime(0)
    coordinator.attach(runtime)
    monkeypatch.setattr(
        coordinator, "refresh_installed_version", lambda **_kwargs: pytest.fail("restart must not probe before handoff"),
    )
    try:
        if not writable_coordination:
            coordinator._coordination_error = "The shared coordination directory is unavailable."
            monkeypatch.setattr(coordinator._store, "locked", lambda: pytest.fail("local restart must not take the shared lock"))
        assert coordinator.prepare_restart(runtime)
        handoff = coordinator.handoff()
        assert (handoff["operation"], handoff["target_version"], handoff["request_id"]) == ("restart", "", "")
        assert store.read_request() is None
        with store.locked():
            assert [record["id"] for record in store.live_records_locked("instances")] == [peer["id"]]
    finally:
        coordinator.close()


def test_manual_restart_cannot_take_over_accepted_upgrade():
    runtime = _Runtime(0)
    coordinator = self_update.UiUpdateCoordinator(lambda: None, current_version="0.3.20", isolated_upgrades=True)
    assert coordinator.prepare(runtime, target_version="0.3.21")
    upgrade = coordinator.operation_response()

    with pytest.raises(self_update.UpdateCheckError, match="update is already in progress"):
        coordinator.prepare_restart(runtime)

    assert coordinator.operation_response() == upgrade
    assert coordinator.handoff()["operation"] == "upgrade"


def test_local_restart_can_retry_without_a_confirmed_version(monkeypatch):
    runtime = _Runtime(1)
    shutdowns = []
    coordinator = self_update.UiUpdateCoordinator(
        lambda: shutdowns.append(True), current_version="0.3.20", isolated_upgrades=True,
    )
    monkeypatch.setattr(coordinator, "_start_monitor", lambda: None)
    monkeypatch.setattr(
        coordinator, "refresh_installed_version", lambda **_kwargs: pytest.fail("restart must not probe before handoff"),
    )
    monkeypatch.setattr(self_update, "SCHEDULED_OPERATION_RETRY_SECONDS", 0)

    assert not coordinator.prepare_restart(runtime, defer_if_busy=True)
    state, scheduled = coordinator.operation_status()
    assert state == "waiting"
    assert scheduled is not None and scheduled["target_version"] == ""
    runtime.active_count = 0
    coordinator._monitor_tick()

    assert shutdowns == [True]
    assert coordinator.handoff()["target_version"] == ""


@pytest.mark.parametrize("visible", ["0.3.20", "0.3.21"])
def test_plain_restart_resolves_and_launches_the_visible_version(tmp_path, monkeypatch, visible):
    probes, replacements = [], []
    release = tmp_path / "release" if visible == "0.3.21" else None

    def select_release(fallback):
        probes.append(fallback)
        return visible, release

    monkeypatch.setattr(self_update, "selected_release", select_release)
    monkeypatch.setattr(self_update, "_replace_current_process", lambda *args: replacements.append(args))
    monkeypatch.setattr(self_update, "run_pip_upgrade", lambda *_args: pytest.fail("restart must not install packages"))

    self_update.restart_ui_after_handoff(
        port=8123, token="private-token", previous_version="0.3.20", request_id="",
        instance_id="", state_dir="", installed_version="", owner=True,
    )

    assert probes == ["0.3.20"]
    [(command, environment)] = replacements
    assert command == self_update.module_command("pyruns.web.app", "--port", "8123", "--no-browser", release=release)
    assert environment[self_update.UI_TOKEN_ENV] == "private-token"
    assert json.loads(environment[self_update.UI_UPDATE_RESULT_ENV]) == {
        "ok": True, "previous_version": "0.3.20", "installed_version": visible, "exit_code": 0, "operation": "restart",
    }


def test_windows_release_launcher_leaves_interrupt_cleanup_to_cli(tmp_path, monkeypatch):
    from pyruns.utils import terminal_capture

    waits = []

    class Child:
        def wait(self, timeout=None):
            waits.append(timeout)
            if timeout is None:
                raise KeyboardInterrupt
            return 130

    monkeypatch.setattr(update_release, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(terminal_capture, "_windows_console_available", lambda: True)
    def launch(_command, **kwargs):
        assert kwargs == {"stdin": sys.stdin, "stdout": sys.stdout, "stderr": sys.stderr}
        return Child()

    monkeypatch.setattr(update_release.subprocess, "Popen", launch)
    assert update_release.launch_cli_release(tmp_path, "pyr", ["run", "training"]) == 130
    assert waits == [None, 30]


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


def _publish_update_request(
    state_dir,
    owner_id: str,
    request_id: str,
    **values,
) -> CoordinationStore:
    store = CoordinationStore(state_dir)
    request = process_record(record_id=owner_id, kind="ui-update")
    request.update({
        "request_id": request_id,
        "owner_instance_id": owner_id,
        "previous_version": "0.3.0",
        **values,
    })
    store.ensure()
    with store.locked():
        store.write_request_locked(request)
    return store


@pytest.mark.parametrize("release_becomes_visible", [True, False])
def test_update_reuses_completed_request_before_release_is_visible(tmp_path, monkeypatch, release_becomes_visible):
    store = _publish_update_request(
        tmp_path / "coordination", "a" * 32, "b" * 32, stage="completed", isolated=True,
        result={"ok": True, "previous_version": "0.3.20", "installed_version": "0.3.21", "exit_code": 0},
    )
    completed = store.read_request()
    clock = 100.0
    restarts = []

    def advance(seconds):
        nonlocal clock
        clock += seconds

    def select_release(_fallback):
        return ("0.3.21", tmp_path / "release") if release_becomes_visible and clock >= 102 else ("0.3.20", None)

    monkeypatch.setattr(self_update.UiUpdateCoordinator, "_start_monitor", lambda self: None)
    monkeypatch.setattr(self_update, "selected_release", select_release)
    monkeypatch.setattr(self_update, "LOCAL_RELEASE_WAIT_SECONDS", 3.0)
    monkeypatch.setattr(self_update.time, "monotonic", lambda: clock)
    monkeypatch.setattr(self_update.time, "sleep", advance)
    monkeypatch.setattr(self_update, "run_pip_upgrade", lambda *_a, **_kw: pytest.fail("a peer must not reinstall"))
    monkeypatch.setattr(self_update, "relaunch_ui", lambda **kwargs: restarts.append(kwargs))
    coordinator = self_update.UiUpdateCoordinator(
        lambda: None, state_dir=store.state_dir, shared=True, current_version="0.3.20", isolated_upgrades=True,
    )
    runtime = _Runtime(0)
    coordinator.attach(runtime)
    try:
        assert coordinator.installed_version == "0.3.20"
        assert coordinator.prepare(runtime, target_version="0.3.21")
        # The monitor must leave the accepted local restart to the HTTP response.
        coordinator._monitor_tick()
        assert coordinator.operation_response()["operation"] == "restart"
        handoff = coordinator.handoff()
        assert handoff["request_id"] == ""
        self_update.restart_ui_after_handoff(
            port=8123, token="browser-token", previous_version="0.3.20",
            request_id=handoff["request_id"], state_dir=handoff["state_dir"], instance_id=handoff["instance_id"],
            installed_version=handoff["target_version"], owner=True,
        )
        result = restarts[0]["result"]
        assert result["operation"] == "restart"
        assert result["ok"] is release_becomes_visible
        assert result["installed_version"] == ("0.3.21" if release_becomes_visible else "0.3.20")
        assert clock == (102.0 if release_becomes_visible else 103.0)
        assert store.read_request() == completed
    finally:
        coordinator.close()


@pytest.mark.parametrize("staged", [True, False])
def test_updater_reuses_release_that_becomes_visible_after_admission(tmp_path, monkeypatch, staged):
    release = tmp_path / "release" if staged else None
    store = _publish_update_request(tmp_path / "state", "a" * 32, "b" * 32, stage="draining", isolated=True)
    monkeypatch.setattr(self_update, "selected_release", lambda _fallback: ("0.3.21", release))
    monkeypatch.setattr(self_update, "create_release_directory", lambda: pytest.fail("no staging directory needed"))
    monkeypatch.setattr(self_update, "run_pip_upgrade", lambda *_a, **_kw: pytest.fail("release is already on disk"))
    result = self_update.run_coordinated_update(
        state_dir=store.state_dir, request_id="b" * 32, instance_id="a" * 32,
        previous_version="0.3.20", target_version="0.3.21",
    )
    assert result["ok"] and result["installed_version"] == "0.3.21"
    assert store.read_request()["result"] == result


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


@pytest.mark.parametrize("completion", ["matching", "missing", "another"])
def test_follower_resumes_only_after_its_update_finishes_before_it_drains(tmp_path, completion):
    state_dir = str(tmp_path / "coordination")
    shutdowns = []
    follower = self_update.UiUpdateCoordinator(
        lambda: shutdowns.append("follower"), shared=True, state_dir=state_dir,
    )
    runtime = _Runtime(1)
    store = _publish_update_request(
        state_dir, "a" * 32, "b" * 32, stage="draining", operation="upgrade",
    )
    follower.attach(runtime)
    try:
        follower._monitor_tick()
        assert follower.requested
        with store.locked():
            if completion == "missing":
                os.remove(store.request_path)
            else:
                request = store.read_request_locked()
                request.update(stage="completed", result={"ok": False, "exit_code": 1})
                if completion == "another":
                    request["request_id"] = "c" * 32
                store.write_request_locked(request)
        follower._monitor_tick()
        if completion == "matching":
            assert follower.state == "idle"
            with follower.task_start_guard():
                pass
        else:
            assert follower.state == "restarting"
            with pytest.raises(UpdateInProgressError):
                with follower.task_start_guard():
                    pass
        assert shutdowns == []
    finally:
        follower.close()


def test_update_coordinator_fails_closed_when_idle_check_fails():
    class BrokenRuntime:
        def active_task_count(self):
            raise OSError("disk unavailable")

    coordinator = self_update.UiUpdateCoordinator(lambda: None)

    with pytest.raises(self_update.UpdateCheckError, match="disk unavailable"):
        coordinator.prepare(BrokenRuntime())
    assert coordinator.requested is False


def test_update_coordinator_uses_strict_runtime_count_before_upgrade():
    class Runtime:
        def active_task_count(self):
            return 0

        def strict_active_task_count(self):
            return 2

    coordinator = self_update.UiUpdateCoordinator(lambda: None)

    with pytest.raises(self_update.ActiveTasksError, match="2 queued or running tasks"):
        coordinator.prepare(Runtime())

    assert coordinator.requested is False


def test_update_coordinator_uses_strict_runtime_count_before_shutdown():
    shutdowns: list[str] = []

    class Runtime:
        def active_task_count(self):
            return 0

        def strict_active_task_count(self):
            return 1

    coordinator = self_update.UiUpdateCoordinator(lambda: shutdowns.append("shutdown"))
    coordinator._runtime = Runtime()

    coordinator._drain_once()

    assert shutdowns == []


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


def test_owner_handoff_refreshes_request_after_slow_runtime_shutdown(tmp_path, monkeypatch):
    state_dir = tmp_path / "coordination"
    owner = self_update.UiUpdateCoordinator(
        lambda: None,
        state_dir=str(state_dir),
        shared=True,
        current_version="0.3.0",
    )
    owner.attach(_Runtime(0))
    try:
        assert owner.prepare(_Runtime(0)) is True
        store = CoordinationStore(state_dir)
        with store.locked():
            request = store.read_request_locked()
        assert request is not None
        initial_heartbeat = float(request["heartbeat_at"])

        now = initial_heartbeat + update_coordination.UPDATE_HANDOFF_GRACE_SECONDS + 1
        monkeypatch.setattr(self_update.time, "time", lambda: now)
        owner.handoff()

        monkeypatch.setattr(
            update_coordination,
            "_record_owner_is_alive",
            lambda _record: False,
        )
        with store.locked():
            active = store.active_request_locked(recover=True)
        assert active is not None
        assert active["request_id"] == request["request_id"]
        assert active["heartbeat_at"] == now
    finally:
        owner.close(failed_handoff=True)


@pytest.mark.parametrize("action", ["close", "handoff"])
def test_ui_retirement_preserves_final_state_after_pending_monitor_write(tmp_path, monkeypatch, action):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    shutdowns = []
    coordinator = self_update.UiUpdateCoordinator(
        lambda: shutdowns.append("shutdown"),
        state_dir=str(tmp_path / "coordination"), shared=True, current_version="0.3.0",
    )
    coordinator.prepare(_Runtime(0))
    store = coordinator._store
    assert store is not None
    entered, release, join_attempted = threading.Event(), threading.Event(), threading.Event()
    monitor = threading.Thread(target=coordinator._monitor_tick, daemon=True)
    coordinator._monitor = monitor
    original_write = store.write_record
    original_join = monitor.join

    def delayed_write(group, identifier, payload):
        if threading.current_thread() is monitor:
            entered.set()
            assert release.wait(5), "pending monitor write was not released"
        return original_write(group, identifier, payload)

    monkeypatch.setattr(store, "write_record", delayed_write)
    # Model the bounded monitor join expiring while its record write is pending.
    monkeypatch.setattr(monitor, "join", lambda timeout: join_attempted.set())
    monitor.start()
    try:
        assert entered.wait(3)
        with ThreadPoolExecutor(max_workers=1) as executor:
            retired = executor.submit(getattr(coordinator, action))
            try:
                assert join_attempted.wait(3)
                with pytest.raises(FutureTimeout):
                    retired.result(timeout=0.1)
            finally:
                release.set()
                original_join(timeout=3)
            retired.result(timeout=3)
        assert not monitor.is_alive()
        records = store.live_records_locked("instances")
        assert [record["phase"] for record in records] == ([] if action == "close" else ["handoff"])
        assert shutdowns == []
    finally:
        release.set()
        original_join(timeout=3)
        coordinator.close(failed_handoff=True)


@pytest.mark.parametrize("legacy_0_3_0", [False, True])
def test_original_updater_resumes_owner_exit_recovery(tmp_path, legacy_0_3_0):
    state_dir = tmp_path / "coordination"
    owner_id = "a" * 32
    request_id = "b" * 32
    store = _publish_update_request(
        state_dir,
        owner_id,
        request_id,
        operation="upgrade",
        stage="draining",
    )
    with store.locked():
        request = store.read_request_locked()
        assert request is not None
        recovered = store.recover_stale_request_locked(request)
        if legacy_0_3_0:
            recovered.pop("recovery_reason")
            store.write_request_locked(recovered)

    self_update._wait_for_update_participants(
        store,
        request_id=request_id,
        instance_id=owner_id,
        previous_version="0.3.0",
    )

    with store.locked():
        resumed = store.read_request_locked()
    assert resumed is not None
    assert resumed["stage"] == "draining"
    assert resumed["owner_instance_id"] == owner_id
    assert "error" not in resumed
    assert "result" not in resumed


def test_updater_does_not_resume_terminal_or_foreign_request(tmp_path):
    state_dir = tmp_path / "coordination"
    owner_id = "a" * 32
    request_id = "b" * 32
    store = _publish_update_request(
        state_dir,
        owner_id,
        request_id,
        operation="upgrade",
        stage="draining",
    )
    with pytest.raises(UpdateCoordinationError, match="no longer active"):
        self_update._wait_for_update_participants(
            store, request_id=request_id, instance_id="c" * 32, previous_version="0.3.0",
        )
    with store.locked():
        request = store.read_request_locked()
        assert request is not None
        store.recover_stale_request_locked(request)

    with pytest.raises(UpdateCoordinationError, match="no longer active"):
        self_update._wait_for_update_participants(
            store,
            request_id=request_id,
            instance_id="c" * 32,
            previous_version="0.3.0",
        )

    with store.locked():
        completed = store.read_request_locked()
        assert completed is not None
        completed.pop("recovery_reason", None)
        completed["error"] = "pip failed"
        store.write_request_locked(completed)
    with pytest.raises(UpdateCoordinationError, match="no longer active"):
        self_update._wait_for_update_participants(
            store,
            request_id=request_id,
            instance_id=owner_id,
            previous_version="0.3.0",
        )


def test_updater_times_out_with_blocking_process_details(tmp_path, monkeypatch, capsys):
    owner_id = "a" * 32
    request_id = "b" * 32
    store = _publish_update_request(
        tmp_path / "coordination",
        owner_id,
        request_id,
        operation="upgrade",
        stage="draining",
    )
    blocker = process_record(record_id="c" * 32, kind="ui")
    blocker.update({"host": "remote-worker", "phase": "serving", "request_id": ""})
    store.write_record("instances", blocker["id"], blocker)
    monkeypatch.setattr(self_update, "UPDATE_PARTICIPANT_WAIT_SECONDS", 0.0)

    with pytest.raises(UpdateCoordinationError, match="remote-worker"):
        self_update._wait_for_update_participants(
            store,
            request_id=request_id,
            instance_id=owner_id,
            previous_version="0.3.0",
        )

    output = capsys.readouterr().out
    assert "Waiting for 1 shared Pyruns process" in output
    assert "remote-worker" in output
    assert "phase=serving" in output


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
        assert owner.requested is False
        assert owner.prepare(_Runtime(0)) is True
        assert owner.requested is True
    finally:
        remote.close()
        owner.close(failed_handoff=True)


def test_shared_restart_can_drain_with_independent_cli_activity(tmp_path, monkeypatch):
    class Runtime(_Runtime):
        def cached_restart_active_task_count(self):
            return 0

        def strict_restart_active_task_count(self):
            return 0

    monkeypatch.setattr(self_update.UiUpdateCoordinator, "_start_monitor", lambda self: None)
    monkeypatch.setattr(self_update.UiUpdateCoordinator, "_installed_version", staticmethod(lambda _fallback: "0.4.0"))
    shutdowns = []
    state_dir = tmp_path / "coordination"
    owner = self_update.UiUpdateCoordinator(
        lambda: shutdowns.append("owner"), state_dir=str(state_dir), shared=True, current_version="0.3.0",
    )
    follower = self_update.UiUpdateCoordinator(
        lambda: shutdowns.append("follower"), state_dir=str(state_dir), shared=True, current_version="0.3.0",
    )
    runtime = Runtime(1)
    owner.attach(runtime)
    follower.attach(runtime)
    try:
        # A peer from an older version publishes only its general active count.
        legacy = process_record(record_id="a" * 32, kind="ui")
        legacy.update(phase="serving", active_count=1)
        owner._store.write_record("instances", legacy["id"], legacy)
        with pytest.raises(self_update.ActiveTasksError):
            owner.prepare_restart(runtime)
        owner._store.remove_record("instances", legacy["id"])

        with EnvironmentActivityLease("cli-runner", state_dir=state_dir):
            with pytest.raises(self_update.ActiveTasksError):
                owner.prepare(runtime)
            assert owner.prepare_restart(runtime)
            owner._monitor_tick()
            follower._monitor_tick()
            assert shutdowns == ["owner", "follower"]
    finally:
        follower.close()
        owner.close(failed_handoff=True)


@pytest.mark.parametrize("operation", ["upgrade", "restart"])
def test_scheduled_operation_checks_installed_version_again(monkeypatch, operation):
    installed = "0.4.0" if operation == "restart" else "0.3.0"
    runtime = _Runtime(1)
    coordinator = self_update.UiUpdateCoordinator(
        lambda: pytest.fail("a different installed release requires confirmation"), current_version="0.3.0",
    )
    monkeypatch.setattr(coordinator, "_installed_version", lambda _fallback: installed)
    monkeypatch.setattr(coordinator, "_start_monitor", lambda: None)
    monkeypatch.setattr(self_update, "SCHEDULED_OPERATION_RETRY_SECONDS", 0)

    if operation == "restart":
        assert not coordinator.prepare_restart(runtime, defer_if_busy=True)
    else:
        assert not coordinator.prepare(runtime, target_version="0.4.0", defer_if_busy=True)
    installed = "0.5.0"
    runtime.active_count = 0
    coordinator._monitor_tick()

    state, scheduled = coordinator.operation_status()
    assert state == "restart_required"
    assert scheduled and "while" in scheduled["error"]
    assert coordinator.requested is False


def test_shared_update_fails_closed_when_record_directory_is_unavailable(
    tmp_path,
    monkeypatch,
):
    state_dir = tmp_path / "coordination"
    coordinator = self_update.UiUpdateCoordinator(
        lambda: None,
        state_dir=str(state_dir),
        shared=True,
        current_version="0.3.0",
    )
    activities_dir = os.path.abspath(str(state_dir / "activities"))
    original_listdir = update_coordination.os.listdir

    def unavailable(directory):
        if os.path.abspath(os.fspath(directory)) == activities_dir:
            raise OSError(errno.ESTALE, "stale file handle")
        return original_listdir(directory)

    monkeypatch.setattr(update_coordination.os, "listdir", unavailable)

    with pytest.raises(self_update.UpdateCheckError, match="Could not coordinate"):
        coordinator.prepare(_Runtime(0))

    assert coordinator.requested is False
    assert CoordinationStore(state_dir).read_request_locked() is None


def test_ui_attach_failure_does_not_bypass_shared_task_guard(tmp_path, monkeypatch):
    state_dir = tmp_path / "coordination"
    coordinator = self_update.UiUpdateCoordinator(
        lambda: None,
        state_dir=str(state_dir),
        shared=True,
        current_version="0.3.0",
    )

    def unavailable(*_args, **_kwargs):
        raise OSError(errno.ESTALE, "stale file handle")

    assert coordinator._store is not None
    monkeypatch.setattr(coordinator._store, "ensure", unavailable)
    coordinator.attach(_Runtime(0))

    try:
        assert coordinator.supported is False
        assert coordinator._monitor is not None
        with pytest.raises(UpdateInProgressError, match="Could not coordinate"):
            with coordinator.task_start_guard():
                pass
    finally:
        coordinator.close()


def test_ui_attach_allows_tasks_only_for_explicit_read_only_storage(tmp_path, monkeypatch):
    state_dir = tmp_path / "coordination"
    coordinator = self_update.UiUpdateCoordinator(
        lambda: None,
        state_dir=str(state_dir),
        shared=True,
        current_version="0.3.0",
    )

    def read_only(*_args, **_kwargs):
        raise OSError(errno.EROFS, "read-only file system")

    assert coordinator._store is not None
    monkeypatch.setattr(coordinator._store, "ensure", read_only)
    coordinator.attach(_Runtime(0))

    assert coordinator.supported is False
    with coordinator.task_start_guard():
        pass


def test_ui_monitor_recovers_after_temporary_coordination_error(tmp_path, monkeypatch):
    coordinator = self_update.UiUpdateCoordinator(
        lambda: None,
        state_dir=str(tmp_path / "coordination"),
        shared=True,
        current_version="0.3.0",
    )
    attempts = 0
    observed_errors: list[bool] = []

    class TwoTickStop:
        def __init__(self) -> None:
            self.waits = 0

        def wait(self, _timeout: float) -> bool:
            self.waits += 1
            return self.waits > 2

    def temporary_failure_then_success() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError(errno.ESTALE, "stale file handle")
        observed_errors.append(not coordinator.supported)

    monkeypatch.setattr(coordinator, "_stop", TwoTickStop())
    monkeypatch.setattr(coordinator, "_monitor_tick", temporary_failure_then_success)

    coordinator._monitor_loop()

    assert attempts == 2
    assert observed_errors == [True]
    assert coordinator.supported is True


def test_update_coordinator_rejects_invalid_or_older_target_versions():
    coordinator = self_update.UiUpdateCoordinator(
        lambda: None,
        current_version="0.3.0",
    )

    with pytest.raises(self_update.UpdateCheckError, match="invalid"):
        coordinator.prepare(_Runtime(0), target_version="not-a-version")
    with pytest.raises(self_update.UpdateCheckError, match="not newer"):
        coordinator.prepare(_Runtime(0), target_version="0.2.0")

    assert coordinator.requested is False


def test_coordinated_update_does_not_run_pip_when_records_are_unavailable(
    tmp_path,
    monkeypatch,
):
    state_dir = tmp_path / "coordination"
    owner_id = "a" * 32
    request_id = "b" * 32
    store = _publish_update_request(
        state_dir,
        owner_id,
        request_id,
        operation="upgrade",
        stage="draining",
    )

    activities_dir = os.path.abspath(store.activities_dir)
    original_listdir = update_coordination.os.listdir

    def unavailable(directory):
        if os.path.abspath(os.fspath(directory)) == activities_dir:
            raise OSError(errno.ESTALE, "stale file handle")
        return original_listdir(directory)

    upgrades: list[str] = []
    monkeypatch.setattr(update_coordination.os, "listdir", unavailable)
    monkeypatch.setattr(
        self_update,
        "run_pip_upgrade",
        lambda version: upgrades.append(version),
    )

    with pytest.raises(UpdateCoordinationError, match="activities records"):
        self_update.run_coordinated_update(
            state_dir=str(state_dir),
            request_id=request_id,
            instance_id=owner_id,
            previous_version="0.3.0",
        )

    assert upgrades == []


def test_strict_coordination_json_rejects_recursion_errors(tmp_path, monkeypatch):
    state_dir = tmp_path / "coordination"
    store = CoordinationStore(state_dir)
    store.ensure()
    (state_dir / "request.json").write_text("{}", encoding="ascii")

    def recursive_loads(*_args, **_kwargs):
        raise RecursionError("JSON nesting is too deep")

    monkeypatch.setattr(update_coordination.json, "loads", recursive_loads)

    with pytest.raises(UpdateCoordinationError, match="invalid"):
        store.read_request_locked()

    assert store.read_request_locked(strict=False) is None


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
    owner_id = "a" * 32
    follower_id = "b" * 32
    request_id = "c" * 32
    store = _publish_update_request(
        state_dir,
        owner_id,
        request_id,
        operation="upgrade",
        stage="draining",
    )
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
    upgrade_targets: list[tuple[str, str]] = []

    def fake_upgrade(previous_version: str, *, target_version: str = ""):
        upgrade_targets.append((previous_version, target_version))
        return dict(result)

    monkeypatch.setattr(self_update.time, "sleep", release_follower)
    monkeypatch.setattr(self_update, "run_pip_upgrade", fake_upgrade)
    monkeypatch.setattr(self_update, "_query_installed_version", lambda _fallback: "0.4.0")

    assert self_update.run_coordinated_update(
        state_dir=str(state_dir),
        request_id=request_id,
        instance_id=owner_id,
        previous_version="0.3.0",
        target_version="0.4.0",
    ) == result
    assert upgrade_targets == [("0.3.0", "0.4.0")]
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


def test_coordinated_restart_accepts_stopped_follower_handoff(tmp_path, monkeypatch):
    state_dir = tmp_path / "coordination"
    owner_id = "a" * 32
    follower_id = "b" * 32
    request_id = "c" * 32
    store = _publish_update_request(
        state_dir,
        owner_id,
        request_id,
        operation="restart",
        stage="draining",
    )
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
    activity = process_record(record_id="d" * 32, kind="cli-runner")
    store.write_record("activities", activity["id"], activity)
    monkeypatch.setattr(self_update, "_query_installed_version", lambda _fallback: "0.4.0")
    monkeypatch.setattr(
        self_update.time,
        "sleep",
        lambda _seconds: pytest.fail("a stopped follower or a CLI runner must not block a restart"),
    )

    relaunches = []
    monkeypatch.setattr(
        self_update,
        "relaunch_ui",
        lambda **kwargs: relaunches.append(kwargs),
    )

    self_update.restart_ui_after_handoff(
        port=8123,
        token="owner-token",
        state_dir=str(state_dir),
        request_id=request_id,
        instance_id=owner_id,
        previous_version="0.3.0",
        installed_version="0.4.0",
        owner=True,
    )

    result = {
        "ok": True,
        "previous_version": "0.3.0",
        "installed_version": "0.4.0",
        "exit_code": 0,
    }
    assert relaunches == [{"port": 8123, "token": "owner-token", "result": result}]
    assert store.read_request()["stage"] == "completed"
    assert store.live_records_locked("activities") == [activity]
    self_update.restart_ui_after_handoff(
        port=8124,
        token="follower-token",
        state_dir=str(state_dir),
        request_id=request_id,
        instance_id=follower_id,
        previous_version="0.3.0",
        installed_version="0.4.0",
        owner=False,
    )
    assert relaunches[-1] == {
        "port": 8124,
        "token": "follower-token",
        "result": result,
    }


def test_staged_upgrade_retains_ownership_through_follower_failure_and_stale_recovery(tmp_path, monkeypatch):
    owner, follower, request_id = "a" * 32, "b" * 32, "c" * 32
    store = _publish_update_request(
        tmp_path / "state", owner, request_id, isolated=True, stage="draining",
    )
    monkeypatch.setattr(update_release, "installation_root", lambda: tmp_path)
    monkeypatch.setattr(self_update, "UPDATE_HEARTBEAT_SECONDS", 3600)
    publish = []
    monkeypatch.setattr(self_update, "publish_release", lambda _path, version: publish.append(version))

    def install(previous_version, *, target_version, install_root):
        self_update._finish_failed_coordination(
            state_dir=store.state_dir, request_id=request_id, instance_id=follower,
            result={"ok": False}, error="follower connection lost",
        )
        with store.locked():
            request = store.read_request_locked()
            assert request is not None and request["stage"] == "updating"
            store.recover_stale_request_locked(request)
        assert install_root.is_dir()
        return {"ok": True, "previous_version": previous_version, "installed_version": target_version, "exit_code": 0}

    monkeypatch.setattr(self_update, "run_pip_upgrade", install)
    result = self_update.run_coordinated_update(
        state_dir=store.state_dir, request_id=request_id, instance_id=owner,
        previous_version="0.3.0", target_version="0.4.0",
    )
    self_update._finish_failed_coordination(
        state_dir=store.state_dir, request_id=request_id, instance_id=owner,
        result={"ok": False}, error="late shutdown failure",
    )
    assert result["ok"] is True
    assert publish == ["0.4.0"]
    assert store.read_request()["result"] == result


@pytest.mark.parametrize("stale_read", [None, {"request_id": "old-request", "stage": "completed"}])
def test_staged_upgrade_retries_request_visibility_before_discard(tmp_path, monkeypatch, stale_read):
    owner, request_id = "a" * 32, "b" * 32
    store = _publish_update_request(
        tmp_path / "state", owner, request_id, isolated=True, stage="draining",
    )
    monkeypatch.setattr(update_release, "installation_root", lambda: tmp_path)
    monkeypatch.setattr(self_update, "UPDATE_HEARTBEAT_SECONDS", 3600)
    monkeypatch.setattr(self_update, "selected_release", lambda _fallback: ("0.3.20", None))
    sleeps, installs, publications, pending_reads = [], [], [], []
    original_read = CoordinationStore.read_request_locked

    def read_request(store, **options):
        if pending_reads:
            return pending_reads.pop(0)
        return original_read(store, **options)

    def install(previous_version, *, target_version, install_root):
        assert install_root.is_dir()
        installs.append(target_version)
        pending_reads.append(stale_read)
        return {"ok": True, "previous_version": previous_version, "installed_version": target_version, "exit_code": 0}

    monkeypatch.setattr(CoordinationStore, "read_request_locked", read_request)
    monkeypatch.setattr(self_update, "run_pip_upgrade", install)
    monkeypatch.setattr(self_update, "publish_release", lambda _root, version: publications.append(version))
    monkeypatch.setattr(self_update.time, "sleep", sleeps.append)
    result = self_update.run_coordinated_update(
        state_dir=store.state_dir, request_id=request_id, instance_id=owner,
        previous_version="0.3.20", target_version="0.3.21",
    )

    assert len(sleeps) == 1
    assert installs == publications == ["0.3.21"]
    assert store.read_request()["result"] == result


@pytest.mark.parametrize("published", [False, True])
def test_staged_upgrade_never_publishes_on_top_of_another_request(tmp_path, monkeypatch, published):
    owner, request_id = "a" * 32, "b" * 32
    newer_request = {"request_id": "c" * 32, "stage": "completed", "result": {"ok": published}}
    store = _publish_update_request(
        tmp_path / "state", owner, request_id, isolated=True, stage="draining",
    )
    monkeypatch.setattr(update_release, "installation_root", lambda: tmp_path)
    monkeypatch.setattr(self_update, "LOCAL_RELEASE_WAIT_SECONDS", 0)
    visible = False
    monkeypatch.setattr(self_update, "selected_release", lambda _fallback: ("0.4.1", tmp_path) if visible else ("0.3.0", None))
    monkeypatch.setattr(self_update, "publish_release", lambda *_args: pytest.fail("the old request must not publish"))

    def install(previous_version, *, target_version, install_root):
        nonlocal visible
        assert install_root.is_dir()
        with store.locked():
            store.write_request_locked(newer_request)
            visible = published
        return {"ok": True, "previous_version": previous_version, "installed_version": target_version, "exit_code": 0}

    monkeypatch.setattr(self_update, "run_pip_upgrade", install)
    arguments = dict(
        state_dir=store.state_dir, request_id=request_id, instance_id=owner,
        previous_version="0.3.0", target_version="0.4.0",
    )
    if published:
        assert self_update.run_coordinated_update(**arguments)["installed_version"] == "0.4.1"
    else:
        with pytest.raises(UpdateCoordinationError, match="no longer owns its request"):
            self_update.run_coordinated_update(**arguments)
    assert store.read_request() == newer_request
    assert not list((tmp_path / ".pyruns-releases").iterdir())


@pytest.mark.parametrize("release_becomes_visible", [True, False])
def test_waiter_checks_local_version_after_shared_success(tmp_path, monkeypatch, release_becomes_visible):
    result = {"ok": True, "previous_version": "0.3.0", "installed_version": "0.4.0", "exit_code": 0}
    store = _publish_update_request(
        tmp_path / "coordination", "a" * 32, "b" * 32, stage="completed", result=result,
    )
    clock = 100.0
    sleeps = []

    def advance(seconds):
        nonlocal clock
        sleeps.append(seconds)
        clock += seconds

    def local_version(_fallback):
        return "0.4.0" if release_becomes_visible and clock >= 102.0 else "0.3.0"

    monkeypatch.setattr(self_update, "LOCAL_RELEASE_WAIT_SECONDS", 3.0)
    monkeypatch.setattr(self_update.time, "monotonic", lambda: clock)
    monkeypatch.setattr(self_update.time, "sleep", advance)
    monkeypatch.setattr(self_update, "_query_installed_version", local_version)

    observed = self_update.wait_for_coordinated_update(
        state_dir=store.state_dir, request_id="b" * 32, instance_id="c" * 32, previous_version="0.3.0",
    )
    if release_becomes_visible:
        assert sleeps == [1.0, 1.0]
        assert observed == result
    else:
        assert sleeps == [1.0, 1.0, 1.0]
        assert observed["ok"] is False and observed["exit_code"] == 124
        assert observed["installed_version"] == "0.3.0"
        assert "could not verify Pyruns 0.4.0 on disk" in observed["error"]
    assert store.read_request()["result"] == result
    assert store.live_records_locked("instances") == []


@pytest.mark.parametrize("first_read", [None, {"request_id": "old-request", "stage": "completed"}])
def test_waiter_retries_a_stale_shared_request_read(tmp_path, monkeypatch, first_read):
    result = {"ok": True, "previous_version": "0.3.0", "installed_version": "0.4.0", "exit_code": 0}
    store = _publish_update_request(
        tmp_path / "coordination", "a" * 32, "b" * 32, stage="completed", result=result,
    )
    responses = iter([first_read, store.read_request()])
    sleeps = []
    monkeypatch.setattr(CoordinationStore, "read_request", lambda _store: next(responses))
    monkeypatch.setattr(self_update.time, "sleep", sleeps.append)
    monkeypatch.setattr(self_update, "_query_installed_version", lambda _fallback: "0.4.0")
    assert self_update.wait_for_coordinated_update(
        state_dir=store.state_dir, request_id="b" * 32, instance_id="c" * 32, previous_version="0.3.0",
    ) == result
    assert sleeps == [0.2]


def test_waiter_reloads_verified_release_even_after_request_replacement(tmp_path, monkeypatch):
    store = _publish_update_request(
        tmp_path / "state", "a" * 32, "b" * 32, stage="completed", result={"ok": False},
    )
    monkeypatch.setattr(self_update, "selected_release", lambda _fallback: ("0.4.1", tmp_path / "release"))
    monkeypatch.setattr(self_update.time, "sleep", lambda _seconds: pytest.fail("the verified release is visible"))
    result = self_update.wait_for_coordinated_update(
        state_dir=store.state_dir, request_id="c" * 32, instance_id="d" * 32,
        previous_version="0.3.0", target_version="0.4.0",
    )
    assert result["ok"] is True and result["installed_version"] == "0.4.1"
    assert store.read_request()["result"] == {"ok": False}


def test_waiter_does_not_load_in_place_metadata_until_shared_install_completes(tmp_path, monkeypatch):
    result = {"ok": True, "previous_version": "0.3.0", "installed_version": "0.4.0", "exit_code": 0}
    store = _publish_update_request(
        tmp_path / "state", "a" * 32, "b" * 32, stage="updating", target_version="0.4.0",
    )
    active = store.read_request()
    responses = iter([active, {**active, "stage": "completed", "result": result}])
    sleeps = []
    monkeypatch.setattr(CoordinationStore, "read_request", lambda _store: next(responses))
    monkeypatch.setattr(self_update, "selected_release", lambda _fallback: ("0.4.0", None))
    monkeypatch.setattr(self_update, "_query_installed_version", lambda _fallback: "0.4.0")
    monkeypatch.setattr(self_update.time, "sleep", sleeps.append)
    assert self_update.wait_for_coordinated_update(
        state_dir=store.state_dir, request_id="b" * 32, instance_id="c" * 32,
        previous_version="0.3.0", target_version="0.4.0",
    ) == result
    assert sleeps == [0.2]


def test_coordinated_waiter_times_out_and_restarts_available_version(tmp_path, monkeypatch):
    owner_id = "a" * 32
    waiter_id = "b" * 32
    request_id = "c" * 32
    _publish_update_request(
        tmp_path / "coordination",
        owner_id,
        request_id,
        operation="upgrade",
        stage="updating",
    )
    monkeypatch.setattr(self_update, "COORDINATED_UPDATE_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(self_update, "_query_installed_version", lambda _fallback: "0.3.0")

    result = self_update.wait_for_coordinated_update(
        state_dir=str(tmp_path / "coordination"),
        request_id=request_id,
        instance_id=waiter_id,
        previous_version="0.3.0",
    )

    assert result == {
        "ok": False,
        "previous_version": "0.3.0",
        "installed_version": "0.3.0",
        "exit_code": 124,
    }


def test_update_never_starts_after_coordination_request_is_lost(monkeypatch):
    monkeypatch.setattr(
        self_update,
        "_heartbeat_coordinated_process",
        lambda *_args, **_kwargs: False,
    )
    heartbeat = self_update._CoordinationHeartbeat(
        CoordinationStore("unused-coordination-state"),
        request_id="request-id",
        instance_id="instance-id",
        previous_version="0.3.3",
        stage="updating",
    )

    with pytest.raises(UpdateCoordinationError, match="no longer active"):
        heartbeat.__enter__()

    assert not heartbeat.thread.is_alive()


def test_activity_lease_rejects_starts_while_shared_gate_is_active(tmp_path):
    state_dir = tmp_path / "coordination"
    owner_id = "d" * 32
    _publish_update_request(
        state_dir,
        owner_id,
        "e" * 32,
        stage="updating",
    )

    with pytest.raises(UpdateInProgressError, match="new tasks are disabled"):
        EnvironmentActivityLease("cli-runner", state_dir=str(state_dir)).start()


def test_activity_lease_disables_coordination_for_read_only_installation(
    tmp_path,
    monkeypatch,
):
    lease = EnvironmentActivityLease(
        "cli-runner",
        state_dir=str(tmp_path / "coordination"),
    )

    def read_only_lock(*_args, **_kwargs):
        raise OSError(errno.EROFS, "read-only file system")

    monkeypatch.setattr(lease.store, "locked", read_only_lock)

    assert lease.start() is lease
    assert lease._disabled is True


def test_activity_lease_fails_closed_when_coordination_lock_times_out(
    tmp_path,
    monkeypatch,
):
    lease = EnvironmentActivityLease(
        "cli-runner",
        state_dir=str(tmp_path / "coordination"),
    )

    def lock_timeout(*_args, **_kwargs):
        raise UpdateCoordinationError("Timed out waiting for the shared Pyruns update lock.")

    monkeypatch.setattr(lease.store, "locked", lock_timeout)

    with pytest.raises(UpdateCoordinationError, match="Timed out"):
        lease.start()

    assert lease._disabled is False
    assert lease._started is False


@pytest.mark.parametrize(
    ("record_id", "error"),
    [
        pytest.param("f" * 32, OSError(errno.ESTALE, "stale file handle"), id="stale"),
        pytest.param(
            "e" * 32,
            FileNotFoundError(errno.ENOENT, "record disappeared"),
            id="disappeared",
        ),
    ],
)
def test_live_records_fail_closed_when_record_is_unavailable(
    tmp_path,
    monkeypatch,
    record_id,
    error,
):
    store = CoordinationStore(tmp_path / "coordination")
    store.write_record(
        "activities",
        record_id,
        process_record(record_id=record_id, kind="cli-runner"),
    )
    record_path = os.path.abspath(store.record_path("activities", record_id))
    original_open = open

    def unavailable(path, *args, **kwargs):
        if os.path.abspath(os.fspath(path)) == record_path:
            raise error
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(update_coordination, "open", unavailable, raising=False)

    with pytest.raises(UpdateCoordinationError, match="Could not read"):
        store.live_records_locked("activities")


def test_strict_request_read_rejects_missing_state_directory(tmp_path):
    store = CoordinationStore(tmp_path / "missing-coordination")

    with pytest.raises(UpdateCoordinationError, match="state directory"):
        store.read_request_locked()

    assert store.read_request_locked(strict=False) is None


def test_activity_lease_cleanup_is_best_effort(tmp_path, monkeypatch):
    lease = EnvironmentActivityLease(
        "cli-runner",
        state_dir=str(tmp_path / "coordination"),
    )
    lease._started = True

    def stale_record(*_args, **_kwargs):
        raise OSError(errno.ESTALE, "stale file handle")

    monkeypatch.setattr(lease.store, "remove_record", stale_record)

    lease.close()

    assert lease._started is False


def test_activity_lease_close_bounds_slow_heartbeat_join(tmp_path):
    lease = EnvironmentActivityLease(
        "cli-runner",
        state_dir=str(tmp_path / "coordination"),
    )
    lease._started = True

    class ThreadStub:
        def __init__(self):
            self.timeout = None

        def join(self, *, timeout):
            self.timeout = timeout

    thread = ThreadStub()
    lease._thread = thread
    lease.close()

    assert lease._started is False
    assert thread.timeout == update_coordination._ACTIVITY_CLOSE_JOIN_SECONDS


def test_remote_shared_leases_survive_brief_disconnects_and_expire(tmp_path):
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


def test_same_hostname_in_another_pid_namespace_keeps_its_shared_lease(tmp_path, monkeypatch):
    store = CoordinationStore(tmp_path / "coordination")
    record_id = "f" * 32
    remote = process_record(record_id=record_id, kind="ui")
    remote["runner_device"] = "another-device"
    store.write_record("instances", record_id, remote)
    monkeypatch.setattr(update_coordination, "local_runner_device", lambda: "this-device")
    monkeypatch.setattr(
        update_coordination, "is_pid_running", lambda _pid: pytest.fail("cannot inspect another device's PID"),
    )
    assert [item["id"] for item in store.live_records_locked("instances")] == [record_id]


def test_local_live_process_keeps_activity_lease_when_heartbeat_is_old(tmp_path):
    store = CoordinationStore(tmp_path / "coordination")
    record_id = "d" * 32
    record = process_record(record_id=record_id, kind="cli-runner")
    record["heartbeat_at"] = time.time() - update_coordination.UPDATE_LEASE_TIMEOUT_SECONDS - 1
    store.write_record("activities", record_id, record)

    with store.locked():
        live = store.live_records_locked("activities")

    assert [item["id"] for item in live] == [record_id]


def test_local_live_update_owner_is_not_recovered_from_old_heartbeat():
    request = process_record(record_id="a" * 32, kind="ui-update")
    request.update(
        {
            "request_id": "b" * 32,
            "stage": "updating",
            "heartbeat_at": time.time()
            - update_coordination.UPDATE_REQUEST_TIMEOUT_SECONDS
            - 1,
        }
    )

    assert CoordinationStore.request_is_stale(request) is False


def test_remote_legacy_coordination_lock_is_not_reclaimed_from_heartbeat(tmp_path):
    store = CoordinationStore(tmp_path / "coordination")
    owner_id = "a" * 32
    owner = process_record(record_id=owner_id, kind="lock")
    owner["host"] = "remote-worker"
    lock_path = store.lock_path
    store.ensure()
    with open(lock_path, "w", encoding="ascii") as handle:
        json.dump(owner, handle)
    os.utime(lock_path, (1, 1))
    store._write_lock_heartbeat(owner_id)
    snapshot = store._lock_snapshot(lock_path)
    assert snapshot is not None
    assert store._lock_is_stale(snapshot) is False
    heartbeat_path = store._lock_heartbeat_path(owner_id)
    assert heartbeat_path is not None
    update_coordination._atomic_write_json(
        heartbeat_path,
        {
            "id": owner_id,
            "heartbeat_at": self_update.time.time()
            - update_coordination.UPDATE_LOCK_STALE_SECONDS
            - 1,
        },
    )
    assert store._lock_is_stale(snapshot) is True
    assert store._lock_reclaimable_without_native(snapshot) is False
    with pytest.raises(UpdateCoordinationError, match="Timed out"):
        with store.locked(timeout=0.01):
            pass


def test_native_coordination_guard_initialization_waits_for_existing_owner(tmp_path):
    store = CoordinationStore(tmp_path / "coordination")
    store.ensure()
    fd = os.open(store.lock_guard_path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = store._try_native_lock(fd)
    try:
        assert acquired is True
        contender = CoordinationStore(store.state_dir)
        with pytest.raises(UpdateCoordinationError, match="Timed out"):
            with contender.locked(timeout=0.05):
                pytest.fail("empty guard allowed another owner")
        assert os.fstat(fd).st_size == 0
    finally:
        store._close_native_lock(fd, acquired)

    with contender.locked(timeout=0.2):
        assert os.path.exists(store.lock_path)
    assert not os.path.exists(store.lock_path)


def test_native_coordination_lock_blocks_stale_heartbeat_reclaim(tmp_path, monkeypatch):
    state_dir = tmp_path / "coordination"
    holder = CoordinationStore(state_dir)
    contender = CoordinationStore(state_dir)
    release = threading.Event()
    entered = threading.Event()
    failures: list[BaseException] = []

    monkeypatch.setattr(update_coordination, "UPDATE_LOCK_POLL_SECONDS", 0.01)
    monkeypatch.setattr(update_coordination, "_record_owner_is_alive", lambda _owner: None)

    def hold_lock():
        try:
            with holder.locked():
                entered.set()
                assert release.wait(timeout=2.0)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert entered.wait(timeout=2.0)
    before = contender._lock_snapshot(contender.lock_path)
    assert before is not None
    try:
        with pytest.raises(UpdateCoordinationError, match="Timed out"):
            with contender.locked(timeout=0.05):
                pass
        assert contender._lock_snapshot(contender.lock_path) == before
    finally:
        release.set()
        thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert failures == []
    assert contender._lock_snapshot(contender.lock_path) is None
    with contender.locked(timeout=0.2):
        pass


def test_orphaned_native_coordination_lock_is_reclaimed(tmp_path):
    store = CoordinationStore(tmp_path / "coordination")
    owner = process_record(record_id="b" * 32, kind="lock")
    owner.update({"host": "remote-worker", "lock_protocol": 2})
    store.ensure()
    with open(store.lock_path, "w", encoding="ascii") as handle:
        json.dump(owner, handle)

    with store.locked(timeout=0.1):
        assert os.path.exists(store.lock_path)


def test_coordination_lock_without_native_guard_uses_legacy_protocol(
    tmp_path,
    monkeypatch,
):
    state_dir = tmp_path / "coordination"
    holder = CoordinationStore(state_dir)
    contender = CoordinationStore(state_dir)
    release = threading.Event()
    entered = threading.Event()
    failures: list[BaseException] = []

    monkeypatch.setattr(holder, "_acquire_native_guard", lambda _deadline: (None, False))
    monkeypatch.setattr(update_coordination, "_record_owner_is_alive", lambda _owner: None)

    def hold_lock():
        try:
            with holder.locked():
                entered.set()
                assert release.wait(timeout=2.0)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert entered.wait(timeout=2.0)
    try:
        snapshot = contender._lock_snapshot(contender.lock_path)
        assert snapshot is not None
        assert "lock_protocol" not in contender._lock_owner(snapshot)
        with pytest.raises(UpdateCoordinationError, match="Timed out"):
            with contender.locked(timeout=0.05):
                pass
    finally:
        release.set()
        thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert failures == []


def test_native_coordination_lock_is_released_after_owner_crash(tmp_path):
    state_dir = tmp_path / "coordination"
    script = (
        "import os, sys; "
        "from pyruns.update_coordination import CoordinationStore; "
        "store = CoordinationStore(sys.argv[1]); "
        "context = store.locked(timeout=1); context.__enter__(); "
        "os._exit(0)"
    )
    subprocess.run(
        [sys.executable, "-c", script, str(state_dir)],
        check=True,
        cwd=os.getcwd(),
    )

    with CoordinationStore(state_dir).locked(timeout=0.5):
        pass


def test_native_coordination_lock_serializes_threads(tmp_path):
    state_dir = tmp_path / "coordination"
    state_lock = threading.Lock()
    active = 0
    maximum = 0
    failures: list[BaseException] = []

    def worker() -> None:
        nonlocal active, maximum
        try:
            with CoordinationStore(state_dir).locked(timeout=2.0):
                with state_lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.02)
                with state_lock:
                    active -= 1
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3.0)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert maximum == 1


def test_coordination_lock_heartbeat_is_removed_after_release(tmp_path, monkeypatch):
    store = CoordinationStore(tmp_path / "coordination")
    monkeypatch.setattr(update_coordination, "UPDATE_LOCK_HEARTBEAT_SECONDS", 0.1)
    heartbeat_written = threading.Event()
    write_heartbeat = store._write_lock_heartbeat

    def observed_write(owner_id):
        write_heartbeat(owner_id)
        heartbeat_written.set()

    monkeypatch.setattr(store, "_write_lock_heartbeat", observed_write)

    with store.locked():
        assert heartbeat_written.wait(timeout=2.0)
        heartbeat_files = list(
            (tmp_path / "coordination").glob("coordination.lock.*.heartbeat")
        )
        assert len(heartbeat_files) == 1

    assert not heartbeat_files[0].exists()


def test_external_install_version_change_waits_for_manual_restart(tmp_path, monkeypatch):
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

        assert coordinator.requested is False
        assert coordinator.restart_required is True
        assert coordinator.installed_version == "0.4.0"
        assert coordinator.state == "restart_required"
        assert shutdowns == []

        assert coordinator.prepare_restart(_Runtime(0)) is True
        coordinator.trigger_shutdown()

        assert coordinator.requested is True
        assert shutdowns == ["shutdown"]
        handoff = coordinator.handoff()
        assert handoff["operation"] == "restart"
        assert handoff["target_version"] == "0.4.0"
    finally:
        coordinator.close(failed_handoff=True)


def test_stale_ui_follows_successful_shared_update_after_its_tasks_finish(tmp_path, monkeypatch):
    installed = "0.3.0"
    shutdowns = []
    runtime = _Runtime(1)
    state_dir = tmp_path / "coordination"
    coordinator = self_update.UiUpdateCoordinator(
        lambda: shutdowns.append("shutdown"), state_dir=str(state_dir), shared=True, current_version="0.3.0",
    )
    monkeypatch.setattr(coordinator, "_start_monitor", lambda: None)
    monkeypatch.setattr(coordinator, "_installed_version", lambda _fallback: installed)
    store = _publish_update_request(
        state_dir, "a" * 32, "b" * 32, stage="completed", operation="upgrade",
        result={"ok": True, "previous_version": "0.3.0", "installed_version": "0.4.0", "exit_code": 0},
    )
    completed = store.read_request()
    coordinator.attach(runtime)
    try:
        coordinator._monitor_tick()
        assert shutdowns == []
        installed = "0.4.0"
        coordinator._next_version_check = 0
        coordinator._monitor_tick()
        assert coordinator.state == "restart_required"
        with coordinator.task_start_guard():
            assert shutdowns == []

        runtime.active_count = 0
        coordinator._monitor_tick()
        assert shutdowns == ["shutdown"]
        handoff = coordinator.handoff()
        assert (handoff["role"], handoff["operation"], handoff["request_id"]) == ("follower", "restart", "b" * 32)
        assert store.read_request() == completed
    finally:
        coordinator.close()


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


@pytest.mark.parametrize(
    "error",
    [
        self_update.urllib.error.URLError("offline"),
        http.client.IncompleteRead(b'{"info":'),
        http.client.BadStatusLine("truncated status"),
    ],
)
def test_check_latest_version_handles_network_failures(monkeypatch, error):
    def offline(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(self_update.urllib.request, "urlopen", offline)
    with pytest.raises(self_update.LatestVersionCheckError, match="check PyPI"):
        self_update.check_latest_version("0.3.0")


def test_check_latest_version_handles_invalid_responses(monkeypatch):
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


@pytest.mark.parametrize("hidden_flags", [{}, {"creationflags": 0x08000000}])
def test_run_pip_upgrade_uses_current_interpreter(monkeypatch, hidden_flags):
    calls = []
    monkeypatch.setattr(self_update, "hidden_subprocess_kwargs", lambda: hidden_flags)

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if "pip" in command:
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0, stdout="0.4.0\n")

    monkeypatch.setattr(self_update.subprocess, "run", fake_run)

    result = self_update.run_pip_upgrade("0.3.0", target_version="0.4.0")

    assert calls[0][0] == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--no-input",
        "--disable-pip-version-check",
        "pyruns>=0.4.0",
    ]
    assert calls[0][1]["stderr"].closed
    assert {key: value for key, value in calls[0][1].items() if key != "stderr"} == {
        "check": False,
        "timeout": self_update.PIP_UPGRADE_TIMEOUT_SECONDS,
        "env": None,
        **hidden_flags,
    }
    assert result == {
        "ok": True,
        "previous_version": "0.3.0",
        "installed_version": "0.4.0",
        "exit_code": 0,
    }


@pytest.mark.parametrize("installed_version", ["0.3.0", "0.3.1"])
def test_run_pip_upgrade_requires_confirmed_target(monkeypatch, installed_version):
    def fake_run(command, **_kwargs):
        if "pip" in command:
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0, stdout=f"{installed_version}\n")

    monkeypatch.setattr(self_update.subprocess, "run", fake_run)

    result = self_update.run_pip_upgrade("0.3.0", target_version="0.4.0")

    assert result == {
        "ok": False,
        "previous_version": "0.3.0",
        "installed_version": installed_version,
        "exit_code": 0,
    }


def test_run_pip_upgrade_failure_keeps_relaunch_result(monkeypatch):
    def fake_run(command, **_kwargs):
        if "pip" in command:
            raise OSError("offline")
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(self_update.subprocess, "run", fake_run)

    result = self_update.run_pip_upgrade("0.3.0", target_version="0.4.0")

    assert result == {
        "ok": False,
        "previous_version": "0.3.0",
        "installed_version": "0.3.0",
        "exit_code": 1,
        "error": "Could not start pip: offline",
    }


def test_run_pip_upgrade_timeout_keeps_relaunch_result(monkeypatch):
    def fake_run(command, **_kwargs):
        if "pip" in command:
            raise subprocess.TimeoutExpired(command, self_update.PIP_UPGRADE_TIMEOUT_SECONDS)
        return SimpleNamespace(returncode=0, stdout="0.3.0\n")

    monkeypatch.setattr(self_update.subprocess, "run", fake_run)

    result = self_update.run_pip_upgrade("0.3.0", target_version="0.4.0")

    assert result == {
        "ok": False,
        "previous_version": "0.3.0",
        "installed_version": "0.3.0",
        "exit_code": 124,
        "error": "pip did not finish within 300 seconds.",
    }


def test_pip_failure_details_survive_restart_result(monkeypatch):
    detail = "ERROR: No matching distribution found for pyruns>=0.4.0"

    def fake_run(command, **kwargs):
        if "pip" in command:
            kwargs["stderr"].write(("warning\n" * 300 + detail).encode())
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0, stdout="0.3.0\n")

    monkeypatch.setattr(self_update.subprocess, "run", fake_run)
    result = self_update.run_pip_upgrade("0.3.0", target_version="0.4.0")
    assert result["ok"] is False
    assert len(result["error"]) <= 1024
    assert result["error"].endswith(detail)
    monkeypatch.setenv(self_update.UI_UPDATE_RESULT_ENV, json.dumps(result))
    assert self_update.read_update_result() == result


def test_waiter_preserves_coordination_failure_details(tmp_path):
    detail = "Timed out waiting for ui@worker pid=42 phase=draining"
    result = {
        "ok": False, "previous_version": "0.3.0", "installed_version": "0.3.0",
        "exit_code": 1,
    }
    store = _publish_update_request(
        tmp_path / "coordination", "a" * 32, "b" * 32,
        stage="completed", result=result, error=detail,
    )
    assert self_update.wait_for_coordinated_update(
        state_dir=store.state_dir, request_id="b" * 32,
        instance_id="c" * 32, previous_version="0.3.0",
    ) == {**result, "error": detail}


def test_process_replacements_keep_token_out_of_command_line(monkeypatch):
    executions = []
    monkeypatch.setattr(self_update, "_WINDOWS_PROCESS_REPLACEMENT", False)
    monkeypatch.setenv("PYRUNS_UI_SESSION_STATE", "session-state.json")
    monkeypatch.setenv("PYRUNS_UI_SESSION_SCOPE", "original-session-scope")

    def fake_execve(executable, command, environment):
        executions.append((executable, command, environment))

    monkeypatch.setattr(self_update.os, "execve", fake_execve)
    monkeypatch.setattr(self_update.sys, "executable", "python-test")

    self_update.replace_process_with_updater(
        port=8123,
        token="private-token",
        previous_version="0.3.0",
        target_version="0.4.0",
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
        "--target-version",
        "0.4.0",
    ]
    assert "private-token" not in updater_exec[1]
    assert updater_exec[2][self_update.UI_TOKEN_ENV] == "private-token"
    assert updater_exec[2]["PYRUNS_UI_SESSION_STATE"] == "session-state.json"
    assert updater_exec[2]["PYRUNS_UI_SESSION_SCOPE"] == "original-session-scope"

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
    assert server_exec[2]["PYRUNS_UI_SESSION_STATE"] == "session-state.json"
    assert server_exec[2]["PYRUNS_UI_SESSION_SCOPE"] == "original-session-scope"
    assert json.loads(server_exec[2][self_update.UI_UPDATE_RESULT_ENV]) == result


def test_windows_process_replacement_waits_for_hidden_child(monkeypatch):
    calls = []
    hidden_flags = {"creationflags": 0x08000000}
    monkeypatch.setattr(self_update, "hidden_subprocess_kwargs", lambda: hidden_flags)
    monkeypatch.setattr(self_update, "_WINDOWS_PROCESS_REPLACEMENT", True)
    monkeypatch.setattr(
        self_update.subprocess,
        "call",
        lambda command, **kwargs: calls.append((command, kwargs)) or 7,
    )

    with pytest.raises(SystemExit) as raised:
        self_update._replace_current_process(
            ["python-test", "-m", "pyruns.web.self_update"],
            {"PYRUNS_TEST": "1"},
        )

    assert raised.value.code == 7
    assert calls == [
        (
            ["python-test", "-m", "pyruns.web.self_update"],
            {
                "env": {"PYRUNS_TEST": "1"},
                "stdin": sys.stdin,
                "stdout": sys.stdout,
                "stderr": sys.stderr,
                **hidden_flags,
            },
        )
    ]


def test_windows_update_processes_hide_console_windows(monkeypatch):
    hidden_flags = {"creationflags": 0x08000000}
    monkeypatch.setattr(self_update, "hidden_subprocess_kwargs", lambda: hidden_flags)
    monkeypatch.setattr(self_update, "_WINDOWS_PROCESS_REPLACEMENT", True)

    replacement_calls = []
    monkeypatch.setattr(
        self_update.subprocess,
        "call",
        lambda command, **kwargs: replacement_calls.append((command, kwargs)) or 0,
    )
    with pytest.raises(SystemExit) as raised:
        self_update._replace_current_process(
            ["python-test", "-m", "pyruns.web.self_update"],
            {"PYRUNS_TEST": "1"},
        )
    assert raised.value.code == 0
    assert replacement_calls[0][1]["creationflags"] == hidden_flags["creationflags"]

    run_calls = []

    def fake_run(command, **kwargs):
        run_calls.append((command, kwargs))
        if "pip" in command:
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0, stdout="0.4.0\n")

    monkeypatch.setattr(self_update.subprocess, "run", fake_run)
    assert self_update.run_pip_upgrade("0.3.0", target_version="0.4.0")["ok"] is True
    assert len(run_calls) == 2
    assert all(
        call_kwargs["creationflags"] == hidden_flags["creationflags"]
        for _command, call_kwargs in run_calls
    )


def test_updater_main_removes_token_before_pip_and_relaunches(monkeypatch):
    observed = {}
    monkeypatch.setenv(self_update.UI_TOKEN_ENV, "private-token")

    def fake_upgrade(previous_version, *, target_version=""):
        observed["pip_token"] = os.environ.get(self_update.UI_TOKEN_ENV)
        observed["previous_version"] = previous_version
        observed["target_version"] = target_version
        return {
            "ok": True,
            "previous_version": previous_version,
            "installed_version": "0.4.0",
            "exit_code": 0,
        }

    def fake_relaunch(**kwargs):
        observed["relaunch"] = kwargs

    monkeypatch.setattr(self_update, "run_pip_upgrade", fake_upgrade)
    monkeypatch.setattr(self_update, "relaunch_ui", fake_relaunch)

    assert self_update.main(
        [
            "--port",
            "8123",
            "--previous-version",
            "0.3.0",
            "--target-version",
            "0.4.0",
        ]
    ) == 1
    assert observed["pip_token"] is None
    assert observed["previous_version"] == "0.3.0"
    assert observed["target_version"] == "0.4.0"
    assert observed["relaunch"]["port"] == 8123
    assert observed["relaunch"]["token"] == "private-token"


def test_restart_only_main_relaunches_without_running_pip(monkeypatch):
    observed = {}
    monkeypatch.setenv(self_update.UI_TOKEN_ENV, "private-token")

    def unexpected_upgrade(*_args, **_kwargs):
        raise AssertionError("a restart-only request must not run pip")

    monkeypatch.setattr(self_update, "run_pip_upgrade", unexpected_upgrade)
    monkeypatch.setattr(
        self_update,
        "relaunch_ui",
        lambda **kwargs: observed.setdefault("relaunch", kwargs),
    )

    assert self_update.main(
        [
            "--port",
            "8123",
            "--previous-version",
            "0.3.0",
            "--restart-only",
            "--installed-version",
            "0.4.0",
        ]
    ) == 1
    assert observed["relaunch"] == {
        "port": 8123,
        "token": "private-token",
        "result": {
            "ok": True,
            "previous_version": "0.3.0",
            "installed_version": "0.4.0",
            "exit_code": 0,
        },
    }


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
            "--target-version",
            "0.4.0",
        ]
    ) == 1
    assert observed["wait"] == {
        "state_dir": "state-dir",
        "request_id": "request-id",
        "instance_id": "instance-id",
        "previous_version": "0.3.0",
        "target_version": "0.4.0",
    }
    assert observed["relaunch"] == {
        "port": 8123,
        "token": "private-token",
        "result": result,
    }


def test_incomplete_waiter_arguments_never_run_upgrade(monkeypatch):
    observed = {}
    monkeypatch.setenv(self_update.UI_TOKEN_ENV, "private-token")
    monkeypatch.setattr(self_update, "_query_installed_version", lambda _fallback: "0.3.0")

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
        "error": "A shared update waiter requires request, instance, and state identifiers.",
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


def test_successful_update_result_requires_the_running_version_to_match(monkeypatch):
    result = {"ok": True, "previous_version": "0.3.0", "installed_version": "0.4.0", "exit_code": 0}
    monkeypatch.setenv(self_update.UI_UPDATE_RESULT_ENV, json.dumps(result))
    assert self_update.read_update_result(running_version="0.4.0") == result

    stale = self_update.read_update_result(running_version="0.3.0")
    assert stale["ok"] is False and stale["installed_version"] == "0.3.0"
    assert "started Pyruns 0.3.0" in stale["error"]
