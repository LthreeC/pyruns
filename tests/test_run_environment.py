import subprocess

import pytest

from pyruns.core import run_environment
from pyruns.core.task_manager import TaskManager
from pyruns.utils.info_io import ensure_run_slot, normalize_run_history, run_slot_count


@pytest.fixture
def inventory(monkeypatch):
    monkeypatch.setattr(run_environment.socket, "gethostname", lambda: "gpu-server-02")
    monkeypatch.setattr(run_environment.platform, "system", lambda: "Linux")
    monkeypatch.setattr(run_environment.platform, "release", lambda: "6.8.0")
    monkeypatch.setattr(run_environment.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(run_environment.platform, "freedesktop_os_release", lambda: {"PRETTY_NAME": "Ubuntu 22.04"})
    monkeypatch.setattr(run_environment.SystemMonitor, "_query_nvidia_smi", lambda *a, **k: (
        '0, GPU-AAA, NVIDIA A100, 81920\n'
        '2, GPU-BBB, "NVIDIA RTX, special", 24576\n'
    ))


def test_snapshot_uses_scheduler_assignment_and_launch_environment(inventory):
    snapshot = run_environment.collect_run_environment(
        ["bash", "train.sh"], {"CUDA_VISIBLE_DEVICES": "2"}, None, assigned_gpu_ids=[2],
    )
    assert snapshot["host"] == "gpu-server-02"
    assert snapshot["system"] == "Ubuntu 22.04 · x86_64"
    assert snapshot["launcher"] == "bash"
    assert snapshot["gpu_scope"] == "assigned"
    assert snapshot["gpus"] == [{
        "index": 2, "uuid": "GPU-BBB", "name": "NVIDIA RTX, special", "memory_total_mb": 24576,
    }]


def test_numeric_cuda_ordinals_are_not_mistaken_for_physical_ids(inventory):
    snapshot = run_environment.collect_run_environment(["python"], {"CUDA_VISIBLE_DEVICES": "0"}, None)
    assert snapshot["gpu_scope"] == "detected"
    assert len(snapshot["gpus"]) == 2
    assert snapshot["cuda_visible_devices"] == "0"


def test_uuid_visibility_selects_the_matching_device(inventory):
    snapshot = run_environment.collect_run_environment(["python"], {"CUDA_VISIBLE_DEVICES": "GPU-BB"}, None)
    assert snapshot["gpu_scope"] == "visible"
    assert [gpu["uuid"] for gpu in snapshot["gpus"]] == ["GPU-BBB"]


@pytest.mark.parametrize("visible", ["", "-1"])
def test_disabled_cuda_is_distinct_from_unrestricted(inventory, monkeypatch, visible):
    def unexpected(*args, **kwargs):
        pytest.fail("Disabled CUDA visibility does not need a device query")
    monkeypatch.setattr(run_environment.SystemMonitor, "_query_nvidia_smi", unexpected)
    snapshot = run_environment.collect_run_environment(["bash"], {"CUDA_VISIBLE_DEVICES": visible}, None)
    assert snapshot["gpu_scope"] == "disabled"
    assert snapshot["gpu_status"] == "ok"
    assert snapshot["gpus"] == []


@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("nvidia-smi", 1)])
def test_gpu_failure_keeps_host_and_launcher(inventory, monkeypatch, failure):
    def unavailable(*args, **kwargs):
        raise failure
    monkeypatch.setattr(run_environment.SystemMonitor, "_query_nvidia_smi", unavailable)
    snapshot = run_environment.collect_run_environment(["bash"], {}, None)
    assert snapshot["host"] == "gpu-server-02"
    assert snapshot["launcher"] == "bash"
    assert snapshot["gpu_status"] == "unavailable"
    assert snapshot["cuda_visible_devices"] is None


def test_wsl_and_conda_launcher_are_explicit(inventory, monkeypatch):
    monkeypatch.setattr(run_environment.platform, "release", lambda: "6.6-microsoft-standard-WSL2")
    snapshot = run_environment.collect_run_environment(
        ["conda", "run", "-n", "llm", "bash", "train.sh"], {}, None, conda_env="llm",
    )
    assert "(WSL)" in snapshot["system"]
    assert snapshot["launcher"] == "conda"
    assert snapshot["conda_env"] == "llm"


def test_history_preserves_old_environments_and_pads_legacy_runs(inventory):
    first = run_environment.collect_run_environment(["bash"], {}, None)
    meta = {"run_index": 2, "start_times": ["first", "second"], "run_environments": [first]}
    assert normalize_run_history(meta) == 2
    assert meta["run_environments"] == [first, None]
    assert ensure_run_slot(meta, 3) == 2
    meta["run_environments"][2] = {**first, "host": "other-server"}
    assert run_slot_count(meta) == 3
    snapshot = TaskManager.serialize_task(meta, summary=True)
    snapshot["run_environments"][0]["gpus"].clear()
    assert meta["run_environments"][0]["gpus"]
    assert meta["run_environments"][2]["host"] == "other-server"
    TaskManager._trim_run_slots(meta, 2)
    assert meta["run_environments"] == [first, None]
