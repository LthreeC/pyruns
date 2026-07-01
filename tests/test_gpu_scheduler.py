from __future__ import annotations

from pathlib import Path

from pyruns.core.executor import _detect_cuda_oom_text
from pyruns.core.gpu_scheduler import (
    GpuDevice,
    GpuResourceScheduler,
    GpuSchedulerConfig,
    SystemGpuProvider,
    format_gpu_queue_block,
)


class SequenceGpuProvider:
    def __init__(self, snapshots: list[list[GpuDevice]]):
        self.snapshots = snapshots
        self.calls = 0

    def sample(self) -> list[GpuDevice]:
        self.calls += 1
        index = min(self.calls - 1, len(self.snapshots) - 1)
        return self.snapshots[index]


def _gpu(index: int, *, used: float, total: float = 40960.0, util: float = 0.0) -> GpuDevice:
    return GpuDevice(
        index=index,
        name=f"GPU {index}",
        uuid=f"GPU-{index}",
        memory_used_mb=used,
        memory_total_mb=total,
        compute_util_pct=util,
    )


def _warm_stable_window(scheduler: GpuResourceScheduler, now: list[float], config: GpuSchedulerConfig) -> None:
    scheduler.snapshot(config)
    deadline = now[0] + config.stable_seconds
    while now[0] < deadline:
        now[0] = min(deadline, now[0] + 1.0)
        scheduler.snapshot(config)


def test_system_gpu_provider_default_monitor_refreshes_within_stable_sample_gap():
    provider = SystemGpuProvider()

    assert provider.monitor._gpu_ttl_sec <= 1.0


def test_gpu_scheduler_reserves_multi_gpu_after_stable_window():
    now = [100.0]
    provider = SequenceGpuProvider([
        [_gpu(0, used=4096, util=0), _gpu(1, used=8192, util=10)],
    ])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: now[0])
    config = GpuSchedulerConfig(
        enabled=True,
        task_mode="multi",
        gpus_per_task=2,
        memory_used_pct=75,
        min_free_memory_gb=8,
        compute_used_pct=30,
        stable_seconds=4,
    )

    first = scheduler.try_reserve("task-a", 1, config, task_env={})
    assert first.assignment is None
    assert "stabilizing 0.0/4s" in first.reason

    second_task = scheduler.try_reserve("task-b", 1, config, task_env={})
    assert second_task.assignment is None
    assert provider.calls == 2

    for timestamp in (101.0, 102.0, 103.0):
        now[0] = timestamp
        warming = scheduler.try_reserve("task-a", 1, config, task_env={})
        assert warming.assignment is None

    now[0] = 104.0
    assigned = scheduler.try_reserve("task-a", 1, config, task_env={})
    assert assigned.assignment is not None
    assert assigned.assignment.gpu_ids == [0, 1]
    assert assigned.assignment.env["CUDA_VISIBLE_DEVICES"] == "0,1"
    assert assigned.assignment.env["PYRUNS_ASSIGNED_GPUS"] == "0,1"

    blocked_by_reservation = scheduler.try_reserve("task-b", 1, config, task_env={})
    assert blocked_by_reservation.assignment is None
    assert "reserved" in blocked_by_reservation.reason

    scheduler.release("task-a")
    released = scheduler.try_reserve("task-b", 1, config, task_env={})
    assert released.assignment is not None
    assert released.assignment.gpu_ids == [0, 1]


def test_gpu_scheduler_synced_reservations_block_new_assignments():
    now = [20.0]
    provider = SequenceGpuProvider([[_gpu(0, used=1024, util=0)]])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: now[0])
    config = GpuSchedulerConfig(enabled=True, task_mode="single", stable_seconds=1)
    _warm_stable_window(scheduler, now, config)

    scheduler.sync_reservations({"remote": [0]})
    decision = scheduler.try_reserve("local", 1, config, task_env={})

    assert decision.assignment is None
    assert "GPU 0 reserved (1/1)" in decision.reason


def test_gpu_scheduler_respects_existing_cuda_visible_devices_when_parseable():
    now = [10.0]
    provider = SequenceGpuProvider([
        [_gpu(0, used=1024, util=0), _gpu(1, used=2048, util=0), _gpu(2, used=1024, util=0)],
    ])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: now[0])
    config = GpuSchedulerConfig(
        enabled=True,
        task_mode="single",
        gpus_per_task=1,
        memory_used_pct=75,
        min_free_memory_gb=8,
        compute_used_pct=30,
        stable_seconds=1,
        respect_cuda_visible_devices=True,
    )
    _warm_stable_window(scheduler, now, config)

    decision = scheduler.try_reserve(
        "manual",
        1,
        config,
        task_env={"CUDA_VISIBLE_DEVICES": "1,2"},
    )

    assert decision.assignment is not None
    assert decision.assignment.gpu_ids == [1, 2]
    assert decision.assignment.cuda_visible_devices == "1,2"
    assert decision.assignment.env == {"PYRUNS_ASSIGNED_GPUS": "1,2"}


def test_gpu_scheduler_respects_existing_cuda_visible_devices_when_unparseable():
    provider = SequenceGpuProvider([[]])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: 10.0)
    config = GpuSchedulerConfig(
        enabled=True,
        task_mode="single",
        stable_seconds=1,
        respect_cuda_visible_devices=True,
    )

    decision = scheduler.try_reserve(
        "manual-mig",
        1,
        config,
        task_env={"CUDA_VISIBLE_DEVICES": "GPU-uuid-0,MIG-GPU-uuid/0/1"},
    )

    assert decision.assignment is not None
    assert decision.assignment.gpu_ids == []
    assert decision.assignment.cuda_visible_devices == "GPU-uuid-0,MIG-GPU-uuid/0/1"
    assert decision.assignment.env == {"PYRUNS_ASSIGNED_GPUS": "GPU-uuid-0,MIG-GPU-uuid/0/1"}


def test_gpu_scheduler_single_mode_skips_blocked_devices_and_prefers_most_free_memory():
    now = [20.0]
    provider = SequenceGpuProvider([
        [
            _gpu(0, used=30000, util=5),
            _gpu(1, used=2048, util=10),
            _gpu(2, used=1024, util=91),
        ],
    ])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: now[0])
    config = GpuSchedulerConfig(
        enabled=True,
        task_mode="single",
        memory_used_pct=75,
        min_free_memory_gb=8,
        compute_used_pct=30,
        stable_seconds=1,
    )
    _warm_stable_window(scheduler, now, config)

    decision = scheduler.try_reserve("single", 1, config, task_env={})

    assert decision.assignment is not None
    assert decision.assignment.gpu_ids == [1]
    assert decision.assignment.env["CUDA_VISIBLE_DEVICES"] == "1"


def test_gpu_scheduler_limits_to_gpu_pool_and_reports_insufficient_multi_gpu_capacity():
    now = [30.0]
    provider = SequenceGpuProvider([
        [
            _gpu(0, used=1024, util=0),
            _gpu(1, used=1024, util=0),
            _gpu(2, used=1024, util=0),
        ],
    ])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: now[0])
    config = GpuSchedulerConfig(
        enabled=True,
        task_mode="multi",
        gpus_per_task=2,
        device_ids=[2],
        memory_used_pct=75,
        min_free_memory_gb=8,
        compute_used_pct=30,
        stable_seconds=1,
    )
    _warm_stable_window(scheduler, now, config)

    decision = scheduler.try_reserve("multi", 1, config, task_env={})

    assert decision.assignment is None
    assert decision.reason == "need 2 eligible GPUs, only 1 available"


def test_gpu_scheduler_multi_mode_can_request_one_gpu_when_limit_is_one():
    now = [35.0]
    provider = SequenceGpuProvider([[_gpu(0, used=1024, util=0)]])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: now[0])
    config = GpuSchedulerConfig(
        enabled=True,
        task_mode="multi",
        gpus_per_task=1,
        memory_used_pct=75,
        min_free_memory_gb=8,
        compute_used_pct=30,
        stable_seconds=1,
    )
    _warm_stable_window(scheduler, now, config)

    decision = scheduler.try_reserve("multi-one", 1, config, task_env={})

    assert decision.assignment is not None
    assert decision.assignment.gpu_ids == [0]
    assert decision.assignment.env["CUDA_VISIBLE_DEVICES"] == "0"


def test_gpu_scheduler_allows_same_gpu_concurrency_until_configured_limit():
    now = [40.0]
    provider = SequenceGpuProvider([[_gpu(0, used=1024, util=0)]])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: now[0])
    config = GpuSchedulerConfig(
        enabled=True,
        task_mode="single",
        memory_used_pct=75,
        min_free_memory_gb=8,
        compute_used_pct=30,
        stable_seconds=1,
        max_tasks_per_gpu=2,
    )
    _warm_stable_window(scheduler, now, config)

    first = scheduler.try_reserve("task-a", 1, config, task_env={})
    second = scheduler.try_reserve("task-b", 1, config, task_env={})
    third = scheduler.try_reserve("task-c", 1, config, task_env={})

    assert first.assignment is not None
    assert first.assignment.gpu_ids == [0]
    assert second.assignment is not None
    assert second.assignment.gpu_ids == [0]
    assert third.assignment is None
    assert third.reason == "GPU 0 reserved (2/2)"

    scheduler.release("task-a")
    after_release = scheduler.try_reserve("task-c", 1, config, task_env={})
    assert after_release.assignment is not None
    assert after_release.assignment.gpu_ids == [0]


def test_gpu_scheduler_waits_when_no_gpu_metrics_are_available():
    provider = SequenceGpuProvider([[]])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: 50.0)
    config = GpuSchedulerConfig(enabled=True, task_mode="single", stable_seconds=1)

    decision = scheduler.try_reserve("no-gpu", 1, config, task_env={})

    assert decision.assignment is None
    assert decision.reason == "no NVIDIA GPU metrics available"


def test_gpu_scheduler_resamples_each_check_but_waits_for_stable_window():
    now = [50.0]
    provider = SequenceGpuProvider([
        [],
        [_gpu(0, used=1024, util=0)],
    ])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: now[0])
    config = GpuSchedulerConfig(
        enabled=True,
        task_mode="single",
        memory_used_pct=75,
        min_free_memory_gb=8,
        compute_used_pct=30,
        stable_seconds=1,
    )

    first = scheduler.try_reserve("no-gpu-a", 1, config, task_env={})
    second = scheduler.try_reserve("no-gpu-b", 1, config, task_env={})

    assert first.assignment is None
    assert second.assignment is None
    assert provider.calls == 2
    assert "stabilizing 0.0/1s" in second.reason

    now[0] = 50.5
    still_warming = scheduler.try_reserve("gpu-warming", 1, config, task_env={})
    assert still_warming.assignment is None
    assert "stabilizing 0.5/1s" in still_warming.reason

    now[0] = 51.0
    scheduler.snapshot(config)
    now[0] = 52.0
    assigned = scheduler.try_reserve("gpu-ready", 1, config, task_env={})

    assert assigned.assignment is not None
    assert assigned.assignment.gpu_ids == [0]


def test_gpu_scheduler_restarts_stable_window_after_unsampled_gap():
    now = [70.0]
    provider = SequenceGpuProvider([
        [_gpu(0, used=1024, util=0)],
    ])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: now[0])
    config = GpuSchedulerConfig(
        enabled=True,
        task_mode="single",
        memory_used_pct=75,
        min_free_memory_gb=8,
        compute_used_pct=30,
        stable_seconds=4,
    )

    first = scheduler.try_reserve("gap", 1, config, task_env={})
    assert first.assignment is None

    now[0] = 74.1
    stale_sample = scheduler.try_reserve("gap", 1, config, task_env={})
    assert stale_sample.assignment is None
    assert "stabilizing 0.0/4s" in stale_sample.reason

    for timestamp in (75.1, 76.1, 77.1):
        now[0] = timestamp
        warming = scheduler.try_reserve("gap", 1, config, task_env={})
        assert warming.assignment is None

    now[0] = 78.1
    assigned = scheduler.try_reserve("gap", 1, config, task_env={})
    assert assigned.assignment is not None
    assert assigned.assignment.gpu_ids == [0]


def test_gpu_scheduler_validates_fixed_cuda_devices_against_pool_and_required_count():
    now = [60.0]
    provider = SequenceGpuProvider([
        [_gpu(0, used=1024, util=0), _gpu(1, used=1024, util=0), _gpu(2, used=1024, util=0)],
    ])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: now[0])
    config = GpuSchedulerConfig(
        enabled=True,
        task_mode="multi",
        gpus_per_task=2,
        device_ids=[0, 1],
        memory_used_pct=75,
        min_free_memory_gb=8,
        compute_used_pct=30,
        stable_seconds=1,
        respect_cuda_visible_devices=True,
    )
    _warm_stable_window(scheduler, now, config)

    too_few = scheduler.try_reserve("manual-one", 1, config, task_env={"CUDA_VISIBLE_DEVICES": "0"})
    outside_pool = scheduler.try_reserve("manual-outside", 1, config, task_env={"CUDA_VISIBLE_DEVICES": "0,2"})

    assert too_few.assignment is None
    assert too_few.reason == "need 2 requested GPUs, only 1 provided"
    assert outside_pool.assignment is None
    assert outside_pool.reason == "GPU 2 outside configured GPU pool"


def test_gpu_scheduler_reports_fixed_cuda_device_block_reason_when_threshold_fails():
    provider = SequenceGpuProvider([
        [_gpu(0, used=30720, total=40960, util=0)],
    ])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: 61.0)
    config = GpuSchedulerConfig(
        enabled=True,
        task_mode="single",
        memory_used_pct=50,
        min_free_memory_gb=8,
        compute_used_pct=30,
        stable_seconds=1,
        respect_cuda_visible_devices=True,
    )

    decision = scheduler.try_reserve("manual-blocked", 1, config, task_env={"CUDA_VISIBLE_DEVICES": "0"})

    assert decision.assignment is None
    assert decision.reason == "GPU 0 memory 75% > 50%"


def test_gpu_scheduler_ignores_blank_cuda_visible_devices_and_assigns_automatically():
    now = [62.0]
    provider = SequenceGpuProvider([
        [_gpu(0, used=1024, util=0)],
    ])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: now[0])
    config = GpuSchedulerConfig(
        enabled=True,
        task_mode="single",
        memory_used_pct=75,
        min_free_memory_gb=8,
        compute_used_pct=30,
        stable_seconds=1,
        respect_cuda_visible_devices=True,
    )
    _warm_stable_window(scheduler, now, config)

    decision = scheduler.try_reserve("blank-cuda", 1, config, task_env={"CUDA_VISIBLE_DEVICES": " , "})

    assert decision.assignment is not None
    assert decision.assignment.gpu_ids == [0]
    assert decision.assignment.cuda_visible_devices == "0"
    assert decision.assignment.env["CUDA_VISIBLE_DEVICES"] == "0"


def test_gpu_queue_log_block_uses_compact_title_and_final_assignment(tmp_path: Path):
    block = format_gpu_queue_block(
        "GPU ASSIGNED",
        [
            "Run #3 assigned GPUs 0,1 after 00:03:42",
            "CUDA_VISIBLE_DEVICES=0,1",
        ],
    )

    assert "[PYRUNS] [GPU ASSIGNED] Run #3 assigned GPUs 0,1 after 00:03:42" in block
    assert "[PYRUNS]   CUDA_VISIBLE_DEVICES=0,1" in block
    assert "=================" not in block


def test_gpu_scheduler_config_parses_string_booleans_from_settings():
    config = GpuSchedulerConfig.from_settings({
        "gpu_scheduler_enabled": "false",
        "gpu_scheduler_respect_cuda_visible_devices": "off",
    })

    assert config.enabled is False
    assert config.respect_cuda_visible_devices is False


def test_gpu_scheduler_config_accepts_truthy_string_booleans():
    config = GpuSchedulerConfig.from_settings({
        "gpu_scheduler_enabled": "yes",
        "gpu_scheduler_respect_cuda_visible_devices": "on",
    })

    assert config.enabled is True
    assert config.respect_cuda_visible_devices is True


def test_gpu_scheduler_config_defaults_match_conservative_local_gpu_profile():
    config = GpuSchedulerConfig.from_settings({})

    assert config.enabled is False
    assert config.task_mode == "single"
    assert config.gpus_per_task == 1
    assert config.device_ids == []
    assert config.memory_used_pct == 40.0
    assert config.min_free_memory_gb == 40.0
    assert config.compute_used_pct == 30.0
    assert config.stable_seconds == 15.0
    assert config.max_wait_seconds == 172800.0
    assert config.max_tasks_per_gpu == 1
    assert config.respect_cuda_visible_devices is True


def test_gpu_scheduler_config_multi_mode_allows_one_gpu_when_limit_is_one():
    config = GpuSchedulerConfig.from_settings({
        "gpu_scheduler_task_mode": "multi",
        "gpu_scheduler_gpus_per_task": 1,
    })

    assert config.gpus_per_task == 1
    assert config.required_gpu_count == 1


def test_gpu_scheduler_config_clamps_percent_thresholds():
    config = GpuSchedulerConfig.from_settings({
        "gpu_scheduler_memory_used_pct": 250,
        "gpu_scheduler_compute_used_pct": -5,
    })

    assert config.memory_used_pct == 100.0
    assert config.compute_used_pct == 0.0


def test_gpu_device_normalizes_metric_fallbacks_and_zero_total_memory():
    device = GpuDevice.from_metric({
        "id": "7",
        "mem_used": "10",
        "mem_total": "0",
        "util": "3",
    })

    assert device.index == 7
    assert device.name == "GPU"
    assert device.memory_used_mb == 10.0
    assert device.memory_used_pct == 100.0
    assert device.free_memory_gb == 0.0


def test_system_gpu_provider_filters_non_dict_metrics_and_non_list_payloads():
    class Monitor:
        def __init__(self, payload):
            self.payload = payload

        def sample(self):
            return self.payload

    assert SystemGpuProvider(Monitor({"gpus": "bad"})).sample() == []
    devices = SystemGpuProvider(Monitor({"gpus": [{"index": 0}, "skip", {"id": 2}]})).sample()

    assert [device.index for device in devices] == [0, 2]


def test_gpu_scheduler_deduplicates_cuda_visible_devices_and_reports_missing_fixed_gpu():
    now = [70.0]
    provider = SequenceGpuProvider([
        [_gpu(0, used=1024, util=0)],
    ])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: now[0])
    config = GpuSchedulerConfig(
        enabled=True,
        task_mode="single",
        memory_used_pct=75,
        min_free_memory_gb=8,
        compute_used_pct=30,
        stable_seconds=1,
        respect_cuda_visible_devices=True,
    )
    _warm_stable_window(scheduler, now, config)

    assigned = scheduler.try_reserve("manual-dup", 1, config, task_env={"CUDA_VISIBLE_DEVICES": "0,0"})
    missing = scheduler.try_reserve("manual-missing", 1, config, task_env={"CUDA_VISIBLE_DEVICES": "3"})

    assert assigned.assignment is not None
    assert assigned.assignment.gpu_ids == [0]
    assert assigned.assignment.cuda_visible_devices == "0,0"
    assert assigned.assignment.env == {"PYRUNS_ASSIGNED_GPUS": "0"}
    assert missing.assignment is None
    assert missing.reason == "GPU 3 unavailable"


def test_gpu_scheduler_config_parses_device_id_variants_and_invalid_values():
    config = GpuSchedulerConfig.from_settings({
        "gpu_scheduler_enabled": "maybe",
        "gpu_scheduler_device_ids": ["0", "0", "x", 2],
        "gpu_scheduler_gpus_per_task": "bad",
        "gpu_scheduler_min_free_memory_gb": -4,
        "gpu_scheduler_stable_seconds": 0,
        "gpu_scheduler_max_wait_seconds": 0,
        "gpu_scheduler_max_tasks_per_gpu": 0,
    })

    assert config.enabled is False
    assert config.device_ids == [0, 2]
    assert config.gpus_per_task == 1
    assert config.min_free_memory_gb == 0.0
    assert config.stable_seconds == 1.0
    assert config.max_wait_seconds == 1.0
    assert config.max_tasks_per_gpu == 1


def test_gpu_scheduler_config_uses_default_for_unparseable_min_free_memory():
    config = GpuSchedulerConfig.from_settings({
        "gpu_scheduler_min_free_memory_gb": "bad",
    })

    assert config.min_free_memory_gb == 40.0


def test_gpu_scheduler_config_parses_auto_device_pool_from_string():
    config = GpuSchedulerConfig.from_settings({
        "gpu_scheduler_device_ids": "auto",
    })

    assert config.device_ids == []


def test_gpu_scheduler_config_ignores_unsupported_device_pool_payloads():
    config = GpuSchedulerConfig.from_settings({
        "gpu_scheduler_device_ids": {"0": True},
    })

    assert config.device_ids == []


def test_gpu_scheduler_reports_memory_free_memory_and_compute_block_reasons():
    memory_config = GpuSchedulerConfig(
        enabled=True,
        task_mode="single",
        memory_used_pct=50,
        min_free_memory_gb=20,
        compute_used_pct=30,
        stable_seconds=1,
    )
    free_config = GpuSchedulerConfig(
        enabled=True,
        task_mode="single",
        memory_used_pct=90,
        min_free_memory_gb=20,
        compute_used_pct=30,
        stable_seconds=1,
    )
    compute_config = GpuSchedulerConfig(
        enabled=True,
        task_mode="single",
        memory_used_pct=90,
        min_free_memory_gb=20,
        compute_used_pct=30,
        stable_seconds=1,
    )

    memory_scheduler = GpuResourceScheduler(
        provider=SequenceGpuProvider([[_gpu(0, used=30720, total=40960, util=0)]]),
        clock=lambda: 80.0,
    )
    free_scheduler = GpuResourceScheduler(
        provider=SequenceGpuProvider([[_gpu(1, used=71680, total=81920, util=0)]]),
        clock=lambda: 80.0,
    )
    compute_scheduler = GpuResourceScheduler(
        provider=SequenceGpuProvider([[_gpu(2, used=1024, total=81920, util=80)]]),
        clock=lambda: 80.0,
    )

    assert "memory 75% > 50%" in memory_scheduler.try_reserve("mem", 1, memory_config, task_env={}).reason
    assert "free 10.0 GiB < 20 GiB" in free_scheduler.try_reserve("free", 1, free_config, task_env={}).reason
    assert "compute 80% > 30%" in compute_scheduler.try_reserve("compute", 1, compute_config, task_env={}).reason


def test_gpu_scheduler_clears_stable_window_for_devices_that_leave_pool():
    now = [90.0]
    provider = SequenceGpuProvider([
        [_gpu(0, used=1024, util=0), _gpu(1, used=1024, util=0)],
    ])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: now[0])
    config_gpu_0 = GpuSchedulerConfig(
        enabled=True,
        task_mode="single",
        device_ids=[0],
        memory_used_pct=75,
        min_free_memory_gb=8,
        compute_used_pct=30,
        stable_seconds=10,
    )
    config_gpu_1 = GpuSchedulerConfig(
        enabled=True,
        task_mode="single",
        device_ids=[1],
        memory_used_pct=75,
        min_free_memory_gb=8,
        compute_used_pct=30,
        stable_seconds=10,
    )

    assert scheduler.try_reserve("warm-0", 1, config_gpu_0, task_env={}).assignment is None
    now[0] = 91.0
    assert scheduler.try_reserve("warm-1", 1, config_gpu_1, task_env={}).assignment is None

    now[0] = 101.0
    stale_gpu_0 = scheduler.try_reserve("stale-0", 1, config_gpu_0, task_env={})
    now[0] = 102.0
    restarted_gpu_1 = scheduler.try_reserve("restarted-1", 1, config_gpu_1, task_env={})
    for timestamp in range(103, 112):
        now[0] = float(timestamp)
        warming_gpu_1 = scheduler.try_reserve(f"warming-1-{timestamp}", 1, config_gpu_1, task_env={})
        assert warming_gpu_1.assignment is None

    now[0] = 112.0
    ready_gpu_1 = scheduler.try_reserve("ready-1", 1, config_gpu_1, task_env={})

    assert stale_gpu_0.assignment is None
    assert "GPU 0 stabilizing" in stale_gpu_0.reason
    assert restarted_gpu_1.assignment is None
    assert "GPU 1 stabilizing" in restarted_gpu_1.reason
    assert ready_gpu_1.assignment is not None
    assert ready_gpu_1.assignment.gpu_ids == [1]


def test_gpu_scheduler_ignores_existing_cuda_visible_devices_when_respect_is_disabled():
    now = [100.0]
    provider = SequenceGpuProvider([
        [_gpu(0, used=1024, util=0), _gpu(1, used=2048, util=0)],
    ])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: now[0])
    config = GpuSchedulerConfig(
        enabled=True,
        task_mode="single",
        memory_used_pct=75,
        min_free_memory_gb=8,
        compute_used_pct=30,
        stable_seconds=1,
        respect_cuda_visible_devices=False,
    )
    _warm_stable_window(scheduler, now, config)

    decision = scheduler.try_reserve(
        "override",
        1,
        config,
        task_env={"CUDA_VISIBLE_DEVICES": "1"},
    )

    assert decision.assignment is not None
    assert decision.assignment.gpu_ids == [0]
    assert decision.assignment.env["CUDA_VISIBLE_DEVICES"] == "0"
    assert decision.assignment.env["PYRUNS_ASSIGNED_GPUS"] == "0"


def test_gpu_scheduler_rejects_blank_and_non_numeric_cuda_visible_devices_as_existing_masks():
    scheduler = GpuResourceScheduler(provider=SequenceGpuProvider([[]]), clock=lambda: 110.0)
    config = GpuSchedulerConfig(enabled=True, task_mode="single", stable_seconds=1)

    blank = scheduler.try_reserve("blank", 1, config, task_env={"CUDA_VISIBLE_DEVICES": "  "})
    non_numeric = scheduler.try_reserve("bad", 1, config, task_env={"CUDA_VISIBLE_DEVICES": "0,gpu"})

    assert blank.assignment is None
    assert blank.reason == "no NVIDIA GPU metrics available"
    assert non_numeric.assignment is not None
    assert non_numeric.assignment.gpu_ids == []
    assert non_numeric.assignment.env == {"PYRUNS_ASSIGNED_GPUS": "0,gpu"}


def test_cuda_oom_text_detection_matches_common_framework_errors():
    assert _detect_cuda_oom_text("torch.cuda.OutOfMemoryError: CUDA out of memory")
    assert _detect_cuda_oom_text("RuntimeError: CUBLAS_STATUS_ALLOC_FAILED")
    assert not _detect_cuda_oom_text("RuntimeError: invalid command line option")
