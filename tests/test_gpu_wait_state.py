from __future__ import annotations

import time

import pytest

from pyruns._config import DEFAULT_ROOT_NAME, ENV_KEY_CLI_TERMINAL_RUNTIME
from pyruns.core.gpu_scheduler import GpuDevice, GpuResourceScheduler, GpuSchedulerConfig
from pyruns.core.task_generator import TaskGenerator
from pyruns.core.task_manager import TaskManager
from pyruns.utils.info_io import load_task_info, update_task_info


class StaticGpuProvider:
    def __init__(self, devices: list[GpuDevice]):
        self.devices = devices
        self.calls = 0

    def sample(self) -> list[GpuDevice]:
        self.calls += 1
        return list(self.devices)


def _config(*, max_wait_seconds: float = 60.0) -> GpuSchedulerConfig:
    return GpuSchedulerConfig(
        enabled=True,
        task_mode="single",
        memory_used_pct=50,
        min_free_memory_gb=8,
        compute_used_pct=30,
        stable_seconds=1,
        max_wait_seconds=max_wait_seconds,
    )


def _create_manager_with_task(tmp_path, monkeypatch, *, config: GpuSchedulerConfig):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-wait", {"lr": 0.1})
    manager = TaskManager(
        tasks_dir=str(tasks_dir),
        lazy_scan=False,
        runner_token="restart-test",
        owns_task_lifecycle=False,
    )
    monkeypatch.setattr(manager, "_gpu_scheduler_config", lambda: config)
    return manager, task, tasks_dir


@pytest.mark.parametrize(
    ("workspace_device", "task_device", "expected_device"),
    [
        ("1", None, 1),
        ("0", "1", 1),
    ],
)
def test_gpu_scheduler_respects_workspace_and_task_cuda_visible_devices(
    tmp_path,
    monkeypatch,
    workspace_device,
    task_device,
    expected_device,
):
    monkeypatch.delenv(ENV_KEY_CLI_TERMINAL_RUNTIME, raising=False)
    workspace = tmp_path / DEFAULT_ROOT_NAME / "main"
    tasks_dir = workspace / "tasks"
    tasks_dir.mkdir(parents=True)
    (workspace.parent / "_pyruns_settings.yaml").write_text(
        f"global_env:\n  CUDA_VISIBLE_DEVICES: '{workspace_device}'\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("env-gpu", {"lr": 0.1})
    manager = TaskManager(
        tasks_dir=str(tasks_dir),
        lazy_scan=False,
        runner_token="env-test",
        owns_task_lifecycle=False,
    )
    config = _config()
    monkeypatch.setattr(manager, "_gpu_scheduler_config", lambda: config)
    if task_device is not None:
        ok, saved_env = manager.update_task_env(
            task["name"],
            {"CUDA_VISIBLE_DEVICES": task_device},
            {},
        )
        assert ok is True
        assert saved_env == {"CUDA_VISIBLE_DEVICES": task_device}

    now = [100.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=StaticGpuProvider(
            [
                GpuDevice(0, "GPU 0", "GPU-0", 1024, 24576, 0),
                GpuDevice(1, "GPU 1", "GPU-1", 2048, 24576, 0),
            ]
        ),
        clock=lambda: now[0],
    )
    assert manager.start_task_now(task["name"]) is True
    manager.gpu_scheduler.snapshot(config, now=now[0])
    now[0] += 1.0

    target, run_index = manager._pick_queued_task()

    assert target is not None
    assert run_index == 1
    assert target["_gpu_assignment"]["gpu_ids"] == [expected_device]
    assert target["_scheduled_env"] == {"PYRUNS_ASSIGNED_GPUS": str(expected_device)}


def test_gpu_wait_state_is_persisted_and_timeout_survives_manager_restart(tmp_path, monkeypatch):
    config = _config(max_wait_seconds=60)
    manager, task, tasks_dir = _create_manager_with_task(tmp_path, monkeypatch, config=config)

    assert manager.start_task_now(task["name"]) is True
    queued_info = load_task_info(task["dir"])
    initial_wait = queued_info["gpu_wait"]
    assert queued_info["status"] == "queued"
    assert queued_info["queued_at"] == initial_wait["started_at"]
    assert initial_wait["deadline_at"] == initial_wait["started_at"] + 60
    assert initial_wait["requested_gpu_count"] == 1

    expired_start = time.time() - 120
    update_task_info(
        task["dir"],
        lambda info: info.update(
            {
                "queued_at": expired_start,
                "gpu_wait": {
                    **info["gpu_wait"],
                    "started_at": expired_start,
                    "deadline_at": expired_start + 60,
                    "updated_at": expired_start,
                },
            }
        ),
    )

    restarted = TaskManager(
        tasks_dir=str(tasks_dir),
        lazy_scan=False,
        runner_token="restart-test",
        owns_task_lifecycle=False,
    )
    monkeypatch.setattr(restarted, "_gpu_scheduler_config", lambda: config)
    restarted.gpu_scheduler = GpuResourceScheduler(provider=StaticGpuProvider([]))

    summary = restarted.get_task(task["name"])
    assert summary["gpu_wait"]["waited_seconds"] >= 119
    assert summary["gpu_wait"]["remaining_seconds"] == 0

    target, run_index = restarted._pick_queued_task()

    assert target is None
    assert run_index == 1
    assert restarted.get_task(task["name"])["status"] == "failed"


def test_gpu_wait_summary_exposes_counts_reason_deadline_and_per_device_details(tmp_path, monkeypatch):
    config = _config()
    manager, task, _tasks_dir = _create_manager_with_task(tmp_path, monkeypatch, config=config)
    assert manager.start_task_now(task["name"]) is True
    provider = StaticGpuProvider([
        GpuDevice(
            index=0,
            name="RTX 5090",
            uuid="GPU-blocked",
            memory_used_mb=23000,
            memory_total_mb=24576,
            compute_util_pct=90,
        )
    ])
    manager.gpu_scheduler = GpuResourceScheduler(provider=provider, clock=lambda: 100.0)

    target, _run_index = manager._pick_queued_task()
    summary = manager.get_task(task["name"])
    wait = summary["gpu_wait"]

    assert target is None
    assert wait["state"] == "waiting"
    assert wait["requested_gpu_count"] == 1
    assert wait["eligible_gpu_count"] == 0
    assert wait["total_gpu_count"] == 1
    assert wait["deadline_at"] > wait["started_at"]
    assert "memory" in wait["reason"]
    assert wait["devices"][0]["uuid"] == "GPU-blocked"
    assert wait["devices"][0]["eligible"] is False
    assert wait["devices"][0]["reason"]

    persisted_wait = load_task_info(task["dir"])["gpu_wait"]
    assert persisted_wait["reason"] == wait["reason"]
    assert persisted_wait["devices"][0]["uuid"] == "GPU-blocked"


def test_gpu_wait_semantic_changes_persist_once_without_time_only_rewrites(tmp_path, monkeypatch):
    config = _config()
    manager, task, _tasks_dir = _create_manager_with_task(tmp_path, monkeypatch, config=config)
    assert manager.start_task_now(task["name"]) is True
    provider = StaticGpuProvider([
        GpuDevice(0, "Busy GPU", "GPU-busy", 23000, 24576, 90),
    ])
    manager.gpu_scheduler = GpuResourceScheduler(provider=provider, clock=lambda: 100.0)

    from pyruns.core import task_manager as task_manager_module

    real_update = task_manager_module.update_task_info
    writes: list[str] = []

    def tracked_update(task_dir, updater, **kwargs):
        writes.append(str(task_dir))
        return real_update(task_dir, updater, **kwargs)

    monkeypatch.setattr(task_manager_module, "update_task_info", tracked_update)

    assert manager._pick_queued_task()[0] is None
    assert len(writes) == 1
    first_persisted = load_task_info(task["dir"])["gpu_wait"]
    assert first_persisted["reason"] == manager.get_task(task["name"])["gpu_wait"]["reason"]

    assert manager._pick_queued_task()[0] is None
    assert len(writes) == 1

    provider.devices = [
        GpuDevice(0, "Busy GPU", "GPU-busy", 22000, 24576, 90),
    ]
    assert manager._pick_queued_task()[0] is None
    assert len(writes) == 1

    provider.devices = [
        GpuDevice(0, "Busy GPU", "GPU-busy", 1000, 24576, 90),
    ]
    assert manager._pick_queued_task()[0] is None
    assert len(writes) == 2
    changed_persisted = load_task_info(task["dir"])["gpu_wait"]
    assert changed_persisted["reason"] != first_persisted["reason"]

    assert manager._pick_queued_task()[0] is None
    assert len(writes) == 2


def test_queue_selection_is_fifo_even_when_new_tasks_are_stored_first(tmp_path, monkeypatch):
    manager = TaskManager(tasks_dir=str(tmp_path / "tasks"), lazy_scan=False, owns_task_lifecycle=False)
    monkeypatch.setattr(manager, "_gpu_scheduler_config", lambda: GpuSchedulerConfig(enabled=False))
    newer = {"name": "newer", "status": "queued", "queued_at": 200.0}
    older = {"name": "older", "status": "queued", "queued_at": 100.0}
    with manager._lock:
        manager.tasks = [newer, older]
        manager._rebuild_indexes_locked()

    target, _run_index = manager._pick_queued_task()

    assert target is older


def test_gpu_queue_pass_samples_and_syncs_once_for_many_blocked_candidates(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False, owns_task_lifecycle=False)
    config = _config()
    monkeypatch.setattr(manager, "_gpu_scheduler_config", lambda: config)
    provider = StaticGpuProvider([
        GpuDevice(0, "Busy GPU", "GPU-busy", 23000, 24576, 90),
    ])
    manager.gpu_scheduler = GpuResourceScheduler(provider=provider, clock=lambda: 200.0)
    sync_calls = []
    loaded_task_info = []
    now = time.time()
    with manager._lock:
        manager.tasks = [
            {
                "name": f"queued-{index}",
                "dir": str(tasks_dir / f"queued-{index}"),
                "status": "queued",
                "queued_at": now + index / 1000,
                "gpu_wait": manager._new_gpu_wait_state(1, config, started_at=now + index / 1000),
                "_gpu_last_wait_log_at": 200.0,
                "env": {},
                "runner_id": manager.runner_id,
                "lease_until": now + 60,
            }
            for index in range(1000)
        ]
        manager._rebuild_indexes_locked()
    task_names = [task["name"] for task in manager.tasks]
    monkeypatch.setattr(manager, "_scan_task_dir_names", lambda: (True, task_names))
    monkeypatch.setattr(
        "pyruns.core.task_manager.load_task_info",
        lambda task_dir: loaded_task_info.append(task_dir) or {},
    )
    original_sync = manager._sync_gpu_reservations_from_running_tasks

    def tracked_sync():
        sync_calls.append(True)
        original_sync()

    monkeypatch.setattr(manager, "_sync_gpu_reservations_from_running_tasks", tracked_sync)

    target, run_index = manager._pick_queued_task()

    assert target is None
    assert run_index == 1
    assert provider.calls == 1
    assert len(sync_calls) == 1
    assert loaded_task_info == []


def test_reservation_sync_skips_unchanged_locally_queued_task_files(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False, owns_task_lifecycle=False)
    with manager._lock:
        manager.tasks = [
            {
                "name": "queued",
                "dir": str(tasks_dir / "queued"),
                "status": "queued",
                "runner_id": manager.runner_id,
                "lease_until": time.time() + 60,
            },
            {
                "name": "running",
                "dir": str(tasks_dir / "running"),
                "status": "running",
                "runner_id": manager.runner_id,
                "lease_until": time.time() + 60,
            },
        ]
        manager._rebuild_indexes_locked()

    monkeypatch.setattr(manager, "_scan_task_dir_names", lambda: (True, ["queued", "running", "unknown"]))
    loaded = []

    def fake_load(task_dir):
        name = str(task_dir).replace("\\", "/").rsplit("/", 1)[-1]
        loaded.append(name)
        return {
            "status": "running",
            "runner_id": manager.runner_id,
            "lease_until": time.time() + 60,
            "_gpu_assignment": {"gpu_ids": [0 if name == "running" else 1]},
        }

    monkeypatch.setattr("pyruns.core.task_manager.load_task_info", fake_load)

    manager._sync_gpu_reservations_from_running_tasks()

    assert loaded == ["running", "unknown"]
    assert manager.gpu_scheduler._reservations == {"running": [0], "unknown": [1]}
