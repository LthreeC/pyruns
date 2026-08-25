import errno
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import pyruns.update_coordination as update_coordination
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

    assert coordinator.supported is False
    with pytest.raises(UpdateInProgressError, match="Could not coordinate"):
        with coordinator.task_start_guard():
            pass


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


def test_coordinated_update_does_not_run_pip_when_records_are_unavailable(
    tmp_path,
    monkeypatch,
):
    state_dir = tmp_path / "coordination"
    store = CoordinationStore(state_dir)
    owner_id = "a" * 32
    request_id = "b" * 32
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


def test_live_records_fail_closed_when_one_record_cannot_be_read(tmp_path, monkeypatch):
    store = CoordinationStore(tmp_path / "coordination")
    record_id = "f" * 32
    store.write_record(
        "activities",
        record_id,
        process_record(record_id=record_id, kind="cli-runner"),
    )
    record_path = os.path.abspath(store.record_path("activities", record_id))
    original_open = open

    def unavailable(path, *args, **kwargs):
        if os.path.abspath(os.fspath(path)) == record_path:
            raise OSError(errno.ESTALE, "stale file handle")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(update_coordination, "open", unavailable, raising=False)

    with pytest.raises(UpdateCoordinationError, match="Could not read"):
        store.live_records_locked("activities")


def test_live_records_fail_closed_when_listed_record_disappears(tmp_path, monkeypatch):
    store = CoordinationStore(tmp_path / "coordination")
    record_id = "e" * 32
    store.write_record(
        "activities",
        record_id,
        process_record(record_id=record_id, kind="cli-runner"),
    )
    record_path = os.path.abspath(store.record_path("activities", record_id))
    original_open = open

    def disappeared(path, *args, **kwargs):
        if os.path.abspath(os.fspath(path)) == record_path:
            raise FileNotFoundError(errno.ENOENT, "record disappeared")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(update_coordination, "open", disappeared, raising=False)

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
