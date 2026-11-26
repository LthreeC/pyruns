"""GPU rule units, admission isolation, and per-submission policy contracts."""

from __future__ import annotations

import time
import json

import pytest

from pyruns.core.gpu_scheduler import GpuDevice, GpuResourceScheduler, GpuSchedulerConfig
from pyruns.core.task_generator import TaskGenerator
from pyruns.core.task_manager import TaskManager
from pyruns.cli.submission_protocol import read_submission_payload, write_submission_payload
from pyruns.utils.gpu_wait import parse_gpu_wait_rule
from pyruns.utils.info_io import load_task_info, update_task_info
from pyruns.utils.settings import load_settings, save_settings_for_root


@pytest.mark.parametrize(
    ("text", "canonical"),
    [
        ("2-20-70-15-2", "2-20-70-15-2"),
        ("2-20480MiB-70%-15SEC-120MIN", "2-20-70-15-2"),
        ("DEVICE=1,0-20GIB-70-0s-0HR", "device=1,0-20-70-0-0"),
        ("OFF", "off"),
        ("1-0.000000001-70-0.000000001-0", "1-0.000000001-70-0.000000001-0"),
        ("1-100000000000000000000-70-0-0", "1-100000000000000000000-70-0-0"),
    ],
)
def test_rule_uses_ui_units_and_accepts_case_insensitive_explicit_units(text, canonical):
    rule = parse_gpu_wait_rule(text)
    assert rule.canonical == canonical
    assert parse_gpu_wait_rule(rule.canonical) == rule


def test_decimal_memory_and_fractional_hours_have_unambiguous_values():
    rule = parse_gpu_wait_rule("1-8GB-90-0.5-0.25")
    assert rule.min_free_memory_gb == pytest.approx(8 * 10**9 / 1024**3)
    assert rule.stable_seconds == 0.5
    assert rule.max_wait_seconds == 900
    assert GpuSchedulerConfig.from_wait_rule("1-8-90-0-0").compute_used_pct == 10


@pytest.mark.parametrize("text", [
    "1-8", "1-8-70", "1-8-70-15", "device=0-8",
    "0-8-70-15-2", "1.5-8-70-15-2", "1-8-101-15-2", "1-8--15-2", "1-8-70-15-2-3",
    "device=0,0-8-70-15-2", "device=-1-8-70-15-2", "1-8TB-70-15-2",
    "1-8-70-NaN-2", "1-8-70-15-1e9",
])
def test_invalid_rules_identify_the_expected_field_order(text):
    with pytest.raises(ValueError, match="GPUS-FREE_GIB-IDLE_PERCENT-STABLE_SECONDS-TIMEOUT_HOURS"):
        parse_gpu_wait_rule(text)


@pytest.mark.parametrize("rule", [None, "2-20480MiB-70%-15SEC-120MIN", "OFF"])
def test_submission_transports_optional_rules_and_retains_legacy_payloads(tmp_path, rule):
    path = tmp_path / "submission.json"
    token = "a" * 32
    write_submission_payload(str(path), token=token, names=["train"], run_indices=[2], gpu_wait_rule=rule)
    payload = read_submission_payload(str(path), token=token)
    assert payload.names == ("train",)
    assert payload.run_indices == (2,)
    assert payload.gpu_wait_rule == (parse_gpu_wait_rule(rule).canonical if rule is not None else None)
    if rule is None:
        assert "gpu_wait_rule" not in json.loads(path.read_text())
    document = json.loads(path.read_text())
    document["gpu_wait_rule"] = {"enabled": True}
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="rule must be text"):
        read_submission_payload(str(path), token=token)


class _GpuProvider:
    def __init__(self, devices):
        self.devices = devices

    def sample(self):
        return list(self.devices)


def test_different_idle_rules_observe_separate_continuous_stability_windows():
    now = [0.0]
    provider = _GpuProvider([])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: now[0])
    loose = GpuSchedulerConfig.from_wait_rule("1-8-0-2-1")
    strict = GpuSchedulerConfig.from_wait_rule("1-8-70-2-1")
    for timestamp, utilization in enumerate([10, 50, 10, 10, 10]):
        now[0] = float(timestamp)
        provider.devices = [GpuDevice(0, "GPU", "GPU-0", 1024, 24576, utilization)]
        scheduler.snapshot(loose, now=now[0])
        for name, config, ready_at in (("loose", loose, 2), ("strict", strict, 4)):
            decision = scheduler.try_reserve(name, 1, config, task_env={}, refresh_snapshot=False)
            assert (decision.assignment is not None) == (timestamp >= ready_at)
            scheduler.release(name)


@pytest.mark.parametrize("selection", ["2", "device=0,1"])
@pytest.mark.parametrize(("blocked_memory", "blocked_util"), [(8192, 10), (1024, 80)])
def test_multi_gpu_wait_requires_every_card_to_remain_eligible(selection, blocked_memory, blocked_util):
    now = [0.0]
    provider = _GpuProvider([])
    scheduler = GpuResourceScheduler(provider=provider, clock=lambda: now[0])
    config = GpuSchedulerConfig.from_wait_rule(f"{selection}-20-70-2-1")

    # GPU 0 stays ready. GPU 1 briefly recovers at t=2, then loses its stable window.
    for timestamp, second_ready in enumerate([False, False, True, False, True, True, True]):
        now[0] = float(timestamp)
        provider.devices = [
            GpuDevice(0, "GPU", "GPU-0", 1024, 24576, 10),
            GpuDevice(
                1, "GPU", "GPU-1",
                1024 if second_ready else blocked_memory, 24576,
                10 if second_ready else blocked_util,
            ),
        ]
        decision = scheduler.try_reserve("two-cards", 1, config, task_env={})
        if timestamp < 6:
            assert decision.assignment is None
        else:
            assert decision.assignment is not None
            assert decision.assignment.gpu_ids == [0, 1]
            assert decision.assignment.env["CUDA_VISIBLE_DEVICES"] == "0,1"


def test_gpu_count_and_explicit_devices_are_respected_inside_visibility_mask():
    provider = _GpuProvider([
        GpuDevice(0, "GPU", "GPU-0", 1024, 24576, 90),
        GpuDevice(1, "GPU", "GPU-1", 1024, 24576, 10),
    ])
    scheduler = GpuResourceScheduler(provider=provider)
    one = scheduler.try_reserve(
        "one", 1, GpuSchedulerConfig.from_wait_rule("1-8-70-0-1"),
        task_env={"CUDA_VISIBLE_DEVICES": "0,1"},
    )
    assert one.assignment.gpu_ids == [1]
    assert one.assignment.env["CUDA_VISIBLE_DEVICES"] == "1"
    scheduler.release("one")
    hidden = scheduler.try_reserve(
        "hidden", 1, GpuSchedulerConfig.from_wait_rule("device=0-8-0-0-1"),
        task_env={"CUDA_VISIBLE_DEVICES": "1"},
    )
    assert hidden.assignment is None
    assert "outside CUDA_VISIBLE_DEVICES" in hidden.reason


@pytest.mark.parametrize("mask", ["", "-1"])
def test_explicit_cuda_mask_that_hides_all_gpus_is_not_overridden(mask):
    scheduler = GpuResourceScheduler(provider=_GpuProvider([
        GpuDevice(0, "GPU", "GPU-0", 1024, 24576, 0),
    ]))
    decision = scheduler.try_reserve(
        "hidden", 1, GpuSchedulerConfig.from_wait_rule("1-8-70-0-1"),
        task_env={"CUDA_VISIBLE_DEVICES": mask},
    )
    assert decision.assignment is None
    assert "CUDA_VISIBLE_DEVICES" in decision.reason


@pytest.mark.parametrize("workers", [1, 2])
def test_blocked_gpu_head_holds_only_its_own_serial_batch(tmp_path, workers):
    generator = TaskGenerator(root_dir=str(tmp_path / "tasks"))
    for name, gpu in (("first", "0"), ("second", "1"), ("other", "1")):
        generator.create_task(name, {}, task_metadata={"env": {"CUDA_VISIBLE_DEVICES": gpu}})
    manager = TaskManager(tasks_dir=str(tmp_path / "tasks"), lazy_scan=False, owns_task_lifecycle=False)
    manager.gpu_scheduler = GpuResourceScheduler(provider=_GpuProvider([
        GpuDevice(0, "A", "GPU-0", 1024, 24576, 95),
        GpuDevice(1, "B", "GPU-1", 1024, 24576, 0),
    ]))
    try:
        rule = "1-8-70-0-0"
        assert manager.start_batch_tasks(["first", "second"], max_workers=workers, gpu_wait_rule=rule) == ["first", "second"]
        manager.scan_disk()
        if workers == 2:
            assert manager._pick_queued_task()[0]["name"] == "second"
        else:
            assert manager._pick_queued_task()[0] is None
            assert manager.start_batch_tasks(["other"], max_workers=2, gpu_wait_rule=rule) == ["other"]
            assert manager._pick_queued_task()[0]["name"] == "other"
            assert manager.cancel_task("first")
            # Once the independent reservation is gone, the next member can run.
            manager._finalize_task_attempt(
                "other", expected_runner_id=manager.runner_id, expected_run_index=1,
                error=RuntimeError("simulated independent worker failure"),
            )
            assert manager._pick_queued_task()[0]["name"] == "second"
    finally:
        manager.shutdown()


@pytest.mark.parametrize("expired", ["first", "second"])
def test_serial_gpu_batch_keeps_the_submission_deadline(tmp_path, monkeypatch, expired):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    generator = TaskGenerator(root_dir=str(tmp_path / "tasks"))
    tasks = {name: generator.create_task(name, {}) for name in ("first", "second")}
    manager = TaskManager(tasks_dir=str(tmp_path / "tasks"), lazy_scan=False, owns_task_lifecycle=False)
    manager.gpu_scheduler = GpuResourceScheduler(provider=_GpuProvider([GpuDevice(0, "A", "GPU-0", 0, 24576, 0)]))
    try:
        assert manager.start_batch_tasks(list(tasks), max_workers=1, gpu_wait_rule="1-8-70-0-1") == list(tasks)
        if expired == "second":
            assert manager._pick_queued_task()[0]["name"] == "first"
        update_task_info(tasks[expired]["dir"], lambda info: info["gpu_wait"].update(deadline_at=time.time() - 1))
        manager.scan_disk()
        if expired == "second":
            assert manager._pick_queued_task()[0] is None
            assert load_task_info(tasks["second"]["dir"])["status"] == "queued"
            manager._finalize_task_attempt(
                "first", expected_runner_id=manager.runner_id, expected_run_index=1, error=RuntimeError("test"),
            )
        assert manager._pick_queued_task()[0] is None
        assert load_task_info(tasks[expired]["dir"])["status"] == "failed"
        if expired == "first":
            assert manager._pick_queued_task()[0]["name"] == "second"
    finally:
        manager.shutdown()


def test_foreign_queued_head_still_precedes_a_recoverable_batch_member(tmp_path):
    generator = TaskGenerator(root_dir=str(tmp_path / "tasks"))
    for name in ("first", "second"):
        generator.create_task(name, {})
    manager = TaskManager(tasks_dir=str(tmp_path / "tasks"), lazy_scan=False, owns_task_lifecycle=False)
    try:
        manager.start_batch_tasks(["first", "second"], max_workers=1, gpu_wait_rule="1-8-70-0-0")
        update_task_info(str(tmp_path / "tasks" / "first"), lambda info: info.update(
            runner_id=manager.runner_id + "-other", lease_until=time.time() + 60,
        ))
        manager.scan_disk()
        with manager._lock:
            assert manager._queued_candidates_locked(independent_only=False) == []
    finally:
        manager.shutdown()


def test_submission_rules_override_project_and_do_not_block_ready_tasks(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    slow = generator.create_task("slow", {})
    ready = generator.create_task("ready", {})
    plain = generator.create_task("plain", {})
    manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False, owns_task_lifecycle=False)
    monkeypatch.setattr(manager, "_submit_task", lambda *args, **kwargs: None)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    manager.gpu_scheduler = GpuResourceScheduler(provider=_GpuProvider([
        GpuDevice(0, "GPU", "GPU-0", 8192, 24576, 20),
    ]))
    try:
        assert manager.start_batch_tasks([slow["name"]], max_workers=3, gpu_wait_rule="2-20-70-0-0") == ["slow"]
        assert manager.start_batch_tasks([ready["name"]], gpu_wait_rule="1-8-70-0-1") == ["ready"]
        selected, run_index = manager._pick_queued_task()
        assert selected["name"] == "ready"
        assert run_index == 1
        assert selected["_scheduled_env"]["CUDA_VISIBLE_DEVICES"] == "0"
        saved = load_task_info(ready["dir"])
        assert "gpu_wait_rule" not in saved
        assert saved["gpu_wait_runs"]["1"]["min_free_memory_gb"] == 8
        # An off task can remain queued behind occupied worker slots, then become runnable.
        assert manager.start_batch_tasks([plain["name"]], max_workers=1, gpu_wait_rule="off") == ["plain"]
        assert load_task_info(plain["dir"])["status"] == "queued"
        unblocked, plain_index = manager._pick_queued_task()
        assert unblocked["name"] == "plain"
        assert plain_index == 1
        assert load_task_info(plain["dir"])["gpu_wait_runs"]["1"]["enabled"] is False
        assert load_task_info(slow["dir"])["status"] == "queued"
        update_task_info(slow["dir"], lambda info: info["gpu_wait"].update({"started_at": time.time() - 1000000}))
        manager.scan_disk()
        assert manager._pick_queued_task()[0] is None
        wait = manager.serialize_task(manager.get_task("slow"))["gpu_wait"]
        assert wait["remaining_seconds"] is None
        assert wait["deadline_at"] is None
    finally:
        manager.shutdown()


@pytest.mark.parametrize(("outcome", "restart"), [
    ("cancelled", "batch"), ("failed", "rerun"), ("cancelled", "immediate"),
])
def test_next_submission_uses_current_project_defaults_after_gpu_wait_ends(
    tmp_path, monkeypatch, outcome, restart,
):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task(
        "train", {}, task_metadata={"gpu_wait_rule": "off"},
    )
    manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False, owns_task_lifecycle=False)
    monkeypatch.setattr(manager, "_submit_task", lambda *args, **kwargs: None)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    manager.gpu_scheduler = GpuResourceScheduler(provider=_GpuProvider([
        GpuDevice(0, "GPU", "GPU-0", 8192, 24576, 20),
    ]))
    try:
        assert manager.start_batch_tasks(["train"], gpu_wait_rule="2-20-70-0-1") == ["train"]
        assert manager._pick_queued_task()[0] is None
        assert load_task_info(task["dir"])["gpu_wait"]["requested_gpu_count"] == 2
        if outcome == "cancelled":
            assert manager.cancel_task("train")
        else:
            update_task_info(
                task["dir"],
                lambda info: info["gpu_wait"].update({"started_at": time.time() - 7200, "deadline_at": time.time() - 1}),
            )
            manager.scan_disk()
            assert manager._pick_queued_task()[0] is None
        assert load_task_info(task["dir"])["status"] == outcome

        # UI settings changed after the previous submission must apply immediately.
        save_settings_for_root(str(tmp_path), {
            "gpu_scheduler_enabled": True,
            "gpu_scheduler_min_free_memory_gb": 12,
            "gpu_scheduler_min_idle_pct": 70,
            "gpu_scheduler_stable_seconds": 0,
        })
        if restart == "batch":
            assert manager.start_batch_tasks(["train"]) == ["train"]
        elif restart == "rerun":
            assert manager.rerun_task("train")
        else:
            assert manager.start_task_now("train")
        queued = load_task_info(task["dir"])
        assert queued["status"] == "queued"
        assert queued["gpu_wait"]["requested_gpu_count"] == 1
        selected, run_index = manager._pick_queued_task()
        assert selected["name"] == "train"
        actual = load_task_info(task["dir"])["gpu_wait_runs"][str(run_index)]
        assert actual["enabled"] is True
        assert actual["min_free_memory_gb"] == 12
        assert actual["gpus_per_task"] == 1
    finally:
        manager.shutdown()


@pytest.mark.parametrize("legacy_rule", ["off", "2-20-70-0-0", "1-8", "bad"])
def test_legacy_task_rule_is_ignored_and_off_only_skips_one_submission(tmp_path, monkeypatch, legacy_rule):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task(
        "train", {}, task_metadata={"gpu_wait_rule": legacy_rule},
    )
    save_settings_for_root(str(tmp_path), {
        "gpu_scheduler_enabled": True,
        "gpu_scheduler_min_free_memory_gb": 12,
        "gpu_scheduler_stable_seconds": 0,
    })
    manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False, owns_task_lifecycle=False)
    monkeypatch.setattr(manager, "_submit_task", lambda *args, **kwargs: None)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    manager.gpu_scheduler = GpuResourceScheduler(provider=_GpuProvider([
        GpuDevice(0, "GPU", "GPU-0", 8192, 24576, 20),
    ]))

    def finish_mock_worker():
        current = load_task_info(task["dir"])
        manager._finalize_task_attempt(
            "train", expected_runner_id=current["runner_id"],
            expected_run_index=current["run_index"],
            error=RuntimeError("simulated worker failure"),
        )
        assert load_task_info(task["dir"])["status"] == "failed"

    try:
        assert manager.start_batch_tasks(["train"]) == ["train"]
        assert load_task_info(task["dir"])["status"] == "queued"
        update_task_info(task["dir"], lambda info: info.update({
            "_gpu_wait_override": {"attempt_id": "stale-attempt", "rule": "off"},
        }))
        manager.scan_disk()
        serialized = manager.serialize_task(manager.get_task("train"))
        assert "_gpu_wait_override" not in serialized
        assert serialized["gpu_wait_rule"] is None
        selected, run_index = manager._pick_queued_task()
        assert selected["name"] == "train"
        assert load_task_info(task["dir"])["gpu_wait_runs"][str(run_index)]["min_free_memory_gb"] == 12
        finish_mock_worker()

        assert manager.start_batch_tasks(["train"], gpu_wait_rule="off") == ["train"]
        assert load_task_info(task["dir"])["status"] == "running"
        finish_mock_worker()

        assert manager.start_batch_tasks(["train"]) == ["train"]
        assert load_task_info(task["dir"])["status"] == "queued"
        selected, run_index = manager._pick_queued_task()
        assert selected["name"] == "train"
        policy = load_task_info(task["dir"])["gpu_wait_runs"][str(run_index)]
        assert policy["enabled"] is True
        assert policy["min_free_memory_gb"] == 12
        assert load_settings(str(tmp_path))["gpu_scheduler_enabled"] is True
    finally:
        manager.shutdown()


def test_old_settings_migrate_idle_threshold_and_retire_hidden_limits(tmp_path):
    settings_path = tmp_path / "_pyruns_settings.yaml"
    settings_path.write_text(
        "gpu_scheduler_compute_used_pct: 10\ngpu_scheduler_memory_used_pct: 1\n"
        "gpu_scheduler_max_tasks_per_gpu: 3\n", encoding="utf-8",
    )
    settings = load_settings(str(tmp_path))
    assert settings["gpu_scheduler_min_idle_pct"] == 90
    config = GpuSchedulerConfig.from_settings(settings)
    assert config.compute_used_pct == 10
    assert config.memory_used_pct == 100
    assert config.max_tasks_per_gpu == 1
    save_settings_for_root(str(tmp_path), {"log_level": "INFO"})
    assert load_settings(str(tmp_path))["gpu_scheduler_min_idle_pct"] == 90
    assert "gpu_scheduler_memory_used_pct" not in settings_path.read_text(encoding="utf-8")
