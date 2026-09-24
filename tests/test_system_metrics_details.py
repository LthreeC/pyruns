import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import psutil

from pyruns.core.system_metrics import SystemMonitor
from pyruns.utils.process_utils import hidden_subprocess_kwargs


@patch("pyruns.core.system_metrics.psutil")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_metrics_skip_gpu_process_query_until_details_are_requested(
    mock_check_output,
    mock_psutil,
):
    mock_psutil.cpu_percent.return_value = 12.0
    mock_psutil.virtual_memory().percent = 34.0
    mock_check_output.return_value = (
        b"0, NVIDIA RTX 5090, GPU-AAA, 5.0, 1024.0, 24576.0\n"
    )

    metrics = SystemMonitor().sample(include_processes=False)

    assert mock_check_output.call_count == 1
    assert mock_check_output.call_args.kwargs == {
        "timeout": SystemMonitor._GPU_QUERY_TIMEOUT_SEC,
        "stderr": subprocess.PIPE,
        **hidden_subprocess_kwargs(),
    }
    assert metrics["gpus"][0]["processes"] == []
    assert "temperature_c" not in metrics["gpus"][0]
    fields = mock_check_output.call_args.args[0][1]
    assert fields == f"--query-gpu={SystemMonitor._GPU_SUMMARY_FIELDS}"
    mock_psutil.Process.assert_not_called()


@patch("pyruns.core.system_metrics.psutil")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_gpu_process_summary_keeps_unavailable_memory_unknown(
    mock_check_output,
    mock_psutil,
):
    mock_psutil.cpu_percent.return_value = 12.0
    mock_psutil.virtual_memory().percent = 34.0
    mock_psutil.Process.return_value.username.return_value = "researcher"
    mock_check_output.side_effect = [
        b"0, NVIDIA RTX 5090, GPU-AAA, 5.0, 1024.0, 24576.0\n",
        b"GPU-AAA, 1234, python.exe, [N/A]\n",
    ]

    metrics = SystemMonitor().sample(include_processes=True)

    process = metrics["gpus"][0]["processes"][0]
    assert process["memory_mb"] is None
    assert process == {
        "pid": 1234,
        "name": "python.exe",
        "user": "researcher",
        "memory_mb": None,
    }
    mock_psutil.Process.assert_called_once_with(1234)


@pytest.mark.parametrize("failure", [psutil.AccessDenied(1234), psutil.NoSuchProcess(1234), OSError("unavailable")])
def test_gpu_summary_owner_is_deduplicated_and_unavailable_owner_keeps_process(failure):
    with patch("pyruns.core.system_metrics.subprocess.check_output", return_value=(
        b"GPU-A, 1234, python, 1024\nGPU-B, 1234, python, 2048\nGPU-A, 5678, train, 512\n"
    )), patch("pyruns.core.system_metrics.psutil.Process") as process:
        process.return_value.username.side_effect = [failure, "researcher"]
        rows = SystemMonitor()._get_gpu_processes()
        assert rows["GPU-A"][0]["user"] == rows["GPU-B"][0]["user"] == "unknown"
        assert rows["GPU-A"][1]["user"] == "researcher"
        assert process.call_count == 2
        assert [call[0] for call in process.return_value.method_calls] == ["username", "username"]


@patch("pyruns.core.system_metrics.psutil.Process")
def test_process_details_expose_bounded_os_metadata(mock_process):
    os_process = MagicMock()
    os_process.username.return_value = "researcher"
    os_process.name.return_value = "python"
    os_process.status.return_value = "sleeping"
    os_process.exe.return_value = "/usr/bin/python"
    os_process.cwd.return_value = "/workspace"
    os_process.cmdline.return_value = ["python", "train.py", "--epochs", "10"]
    os_process.create_time.return_value = 1_725_000_000.0
    os_process.memory_info.return_value = SimpleNamespace(rss=1_610_612_736)
    os_process.memory_percent.return_value = 6.25
    os_process.num_threads.return_value = 12
    os_process.ppid.return_value = 1000
    mock_process.return_value = os_process

    details = SystemMonitor.get_process_details(1234)

    assert details["pid"] == 1234
    assert details["available"] is True
    assert details["user"] == "researcher"
    assert details["process_name"] == "python"
    assert details["status"] == "sleeping"
    assert details["executable"] == "/usr/bin/python"
    assert "train.py" in details["command_line"]
    assert "--epochs" in details["command_line"]
    assert details["command_line_truncated"] is False
    assert details["working_directory"] == "/workspace"
    assert details["created_at"] == 1_725_000_000.0
    assert details["host_memory_mb"] == 1536.0
    assert details["host_memory_percent"] == 6.25
    assert details["thread_count"] == 12
    assert details["parent_pid"] == 1000
    mock_process.assert_called_once_with(1234)


@patch("pyruns.core.system_metrics.psutil.Process")
def test_process_details_bound_abnormally_long_command_lines(mock_process):
    os_process = MagicMock()
    os_process.cmdline.return_value = [
        "python",
        "x" * (SystemMonitor._PROCESS_COMMAND_LIMIT + 100),
    ]
    mock_process.return_value = os_process

    details = SystemMonitor._process_details(1234)

    assert len(details["command_line"]) == SystemMonitor._PROCESS_COMMAND_LIMIT
    assert details["command_line_truncated"] is True


@patch("pyruns.core.system_metrics.psutil.Process")
def test_process_details_report_when_process_has_exited(mock_process):
    mock_process.side_effect = ProcessLookupError("process exited")

    details = SystemMonitor.get_process_details(1234)

    assert details["pid"] == 1234
    assert details["available"] is False
    assert details["command_line"] == ""


def test_process_details_reject_invalid_pid():
    with pytest.raises(ValueError, match="positive integer"):
        SystemMonitor.get_process_details(0)


@patch("pyruns.core.system_metrics.psutil")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_metrics_expose_detailed_gpu_device_fields(
    mock_check_output,
    mock_psutil,
):
    mock_psutil.cpu_percent.return_value = 12.0
    mock_psutil.virtual_memory().percent = 34.0
    mock_check_output.return_value = (
        b"0, NVIDIA RTX 5090, GPU-AAA, 75, 8192, 24576, 42, 16384, "
        b"67, 35, 210.5, 575, P2, Default, 2520, 14001, "
        b"00000000:2B:00.0, 590.12\n"
    )

    gpu = SystemMonitor().sample(
        include_processes=False,
        detail=True,
    )["gpus"][0]

    assert gpu == {
        "id": 0,
        "index": 0,
        "name": "NVIDIA RTX 5090",
        "uuid": "GPU-AAA",
        "util": 75.0,
        "mem_used": 8192.0,
        "mem_total": 24576.0,
        "mem_util": 42.0,
        "mem_free": 16384.0,
        "temperature_c": 67.0,
        "fan_speed_pct": 35.0,
        "power_draw_w": 210.5,
        "power_limit_w": 575.0,
        "performance_state": "P2",
        "compute_mode": "Default",
        "graphics_clock_mhz": 2520.0,
        "memory_clock_mhz": 14001.0,
        "pci_bus_id": "00000000:2B:00.0",
        "driver_version": "590.12",
        "processes": [],
    }
    fields = mock_check_output.call_args.args[0][1]
    assert fields == f"--query-gpu={SystemMonitor._GPU_DETAIL_FIELDS}"


@patch("pyruns.core.system_metrics.psutil")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_metrics_fall_back_when_driver_rejects_detailed_query(
    mock_check_output,
    mock_psutil,
):
    mock_psutil.cpu_percent.return_value = 12.0
    mock_psutil.virtual_memory().percent = 34.0
    mock_check_output.side_effect = [
        subprocess.CalledProcessError(1, ["nvidia-smi"]),
        b"0, NVIDIA RTX 4090, GPU-AAA, 45, 4000, 8000\n",
    ]

    monitor = SystemMonitor(gpu_ttl_sec=60)
    gpu = monitor.sample(
        include_processes=False,
        detail=True,
    )["gpus"][0]
    cached_gpu = monitor.sample(
        include_processes=False,
        detail=True,
    )["gpus"][0]

    assert gpu["name"] == "NVIDIA RTX 4090"
    assert "temperature_c" not in gpu
    assert "driver_version" not in gpu
    assert cached_gpu == gpu
    assert mock_check_output.call_count == 2
    assert mock_check_output.call_args_list[1].args[0][1] == (
        f"--query-gpu={SystemMonitor._GPU_SUMMARY_FIELDS}"
    )


@patch("pyruns.core.system_metrics.psutil")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_summary_and_detail_gpu_snapshots_use_separate_query_depth(
    mock_check_output,
    mock_psutil,
):
    mock_psutil.cpu_percent.return_value = 12.0
    mock_psutil.virtual_memory().percent = 34.0
    mock_check_output.side_effect = [
        b"0, NVIDIA RTX 5090, GPU-AAA, 5, 1024, 24576\n",
        (
            b"0, NVIDIA RTX 5090, GPU-AAA, 5, 1024, 24576, 4, "
            b"23552, 45, 20, 100, 575, P8, Default, 300, 405, "
            b"00000000:2B:00.0, 590.12\n"
        ),
    ]
    monitor = SystemMonitor(gpu_ttl_sec=60)

    summary = monitor.sample(include_processes=False, detail=False)
    detailed = monitor.sample(include_processes=False, detail=True)
    cached_summary = monitor.sample(include_processes=False, detail=False)

    assert "temperature_c" not in summary["gpus"][0]
    assert detailed["gpus"][0]["temperature_c"] == 45.0
    assert "temperature_c" not in cached_summary["gpus"][0]
    assert [call.args[0][1] for call in mock_check_output.call_args_list] == [
        f"--query-gpu={SystemMonitor._GPU_SUMMARY_FIELDS}",
        f"--query-gpu={SystemMonitor._GPU_DETAIL_FIELDS}",
    ]
    mock_psutil.Process.assert_not_called()


@patch("pyruns.core.system_metrics.time.monotonic")
@patch("pyruns.core.system_metrics.psutil")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_detail_request_reads_processes_when_gpu_refresh_fails(
    mock_check_output,
    mock_psutil,
    mock_monotonic,
):
    mock_psutil.cpu_percent.return_value = 12.0
    mock_psutil.virtual_memory().percent = 34.0
    mock_monotonic.return_value = 10.0
    mock_check_output.side_effect = [
        b"0, NVIDIA RTX 5090, GPU-AAA, 5.0, 1024.0, 24576.0\n",
        OSError("nvidia-smi temporarily unavailable"),
        b"GPU-AAA, 1234, python, 2048\n",
    ]
    monitor = SystemMonitor(gpu_ttl_sec=1.5)

    summary_gpu = monitor.sample(include_processes=False)["gpus"][0]
    mock_monotonic.return_value = 12.0
    detail_gpu = monitor.sample(include_processes=True)["gpus"][0]

    assert summary_gpu["processes"] == []
    assert detail_gpu["uuid"] == "GPU-AAA"
    assert detail_gpu["processes"][0]["pid"] == 1234
    assert "processes_error" not in detail_gpu


@pytest.mark.parametrize("failure", [
    subprocess.TimeoutExpired("nvidia-smi", 5),
    subprocess.CalledProcessError(1, "nvidia-smi", stderr=b"Insufficient Permissions"),
    OSError("nvidia-smi temporarily unavailable"),
])
@pytest.mark.parametrize("has_previous", [False, True])
def test_gpu_process_query_failure_is_visible_and_retry_recovers(failure, has_previous):
    monitor = SystemMonitor(gpu_ttl_sec=0)
    device = b"0, NVIDIA RTX 5090, GPU-AAA, 75, 8192, 24576\n"
    processes = b"GPU-AAA, 1234, python, 2048\n"
    with patch("pyruns.core.system_metrics.subprocess.check_output") as query:
        if has_previous:
            query.side_effect = [device, processes]
            monitor.sample()
        query.side_effect = [device, failure]
        failed = monitor.sample()["gpus"][0]
        assert failed["processes_error"]
        assert [process["pid"] for process in failed["processes"]] == ([1234] if has_previous else [])
        query.side_effect = [device, processes]
        recovered = monitor.sample()["gpus"][0]
        assert "processes_error" not in recovered
        assert recovered["processes"][0]["pid"] == 1234
        query.side_effect = [device, b""]
        empty = monitor.sample()["gpus"][0]
        assert "processes_error" not in empty
        assert empty["processes"] == []


def test_slow_gpu_query_cache_lasts_from_query_completion(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("pyruns.core.system_metrics.time", SimpleNamespace(monotonic=lambda: now[0]))
    monitor = SystemMonitor(gpu_ttl_sec=0.25)

    def slow_query(*, detail):
        now[0] += 0.5
        return "0, GPU, GPU-AAA, 5, 1024, 24576\n", False

    with patch.object(monitor, "_query_gpu_snapshot", side_effect=slow_query) as query:
        first = monitor._get_gpu_metrics(include_processes=False)
        assert monitor._get_gpu_metrics(include_processes=False) == first
        assert query.call_count == 1

        now[0] += 0.3
        assert monitor._get_gpu_metrics(include_processes=False) == first
        assert query.call_count == 2


def test_slow_gpu_process_query_is_cached_after_completion(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("pyruns.core.system_metrics.time", SimpleNamespace(monotonic=lambda: now[0]))
    monitor = SystemMonitor(gpu_ttl_sec=1.5)

    def slow_processes():
        now[0] += 4.0
        return {"GPU-AAA": [{"pid": 1234, "memory_mb": 2048}]}

    with (
        patch.object(monitor, "_query_gpu_snapshot", return_value=("0, GPU, GPU-AAA, 5, 1024, 24576\n", False)),
        patch.object(monitor, "_get_gpu_processes", side_effect=slow_processes) as query,
    ):
        first = monitor._get_gpu_metrics(detail=False)
        assert first[0]["processes"][0]["pid"] == 1234
        assert monitor._get_gpu_metrics(detail=False) == first
        assert query.call_count == 1

        now[0] += 1.6
        assert monitor._get_gpu_metrics(detail=False) == first
        assert query.call_count == 2


def test_gpu_disable_cooldown_starts_when_failed_query_finishes(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("pyruns.core.system_metrics.time", SimpleNamespace(monotonic=lambda: now[0]))
    monitor = SystemMonitor()
    monitor._gpu_max_fails = 1

    def slow_failure(*, detail):
        now[0] += 2.0
        raise OSError("NVIDIA temporarily unavailable")

    with patch.object(monitor, "_query_gpu_snapshot", side_effect=slow_failure) as query:
        assert monitor._get_gpu_metrics(include_processes=False) == []
        assert query.call_count == 1

        now[0] += monitor._gpu_retry_sec - 1.0
        assert monitor._get_gpu_metrics(include_processes=False) == []
        assert query.call_count == 1

        now[0] += 2.0
        assert monitor._get_gpu_metrics(include_processes=False) == []
        assert query.call_count == 2


def test_gpu_summary_does_not_wait_for_slow_process_details():
    monitor = SystemMonitor(gpu_ttl_sec=60)
    process_query_started = threading.Event()
    release_process_query = threading.Event()
    summary_ready = threading.Event()

    def slow_processes():
        process_query_started.set()
        assert release_process_query.wait(2)
        return {"GPU-AAA": [{"pid": 1234, "memory_mb": 2048}]}

    def request_summary():
        result = monitor._get_gpu_metrics(include_processes=False)
        summary_ready.set()
        return result

    with (
        patch.object(monitor, "_query_gpu_snapshot", return_value=("0, GPU, GPU-AAA, 5, 1024, 24576\n", False)),
        patch.object(monitor, "_get_gpu_processes", side_effect=slow_processes),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        details = pool.submit(monitor._get_gpu_metrics, detail=False)
        try:
            assert process_query_started.wait(2)
            summary = pool.submit(request_summary)
            summary_finished_before_details = summary_ready.wait(0.5)
        finally:
            release_process_query.set()
        assert details.result(timeout=2)[0]["processes"][0]["pid"] == 1234
        assert summary.result(timeout=2)[0]["processes"] == []

    assert summary_finished_before_details


def test_concurrent_gpu_details_share_one_process_query():
    monitor = SystemMonitor(gpu_ttl_sec=60)
    process_query_started = threading.Event()
    second_request_started = threading.Event()
    duplicate_query = threading.Event()
    release_process_query = threading.Event()
    query_count = 0
    count_lock = threading.Lock()

    def slow_processes():
        nonlocal query_count
        with count_lock:
            query_count += 1
            if query_count > 1:
                duplicate_query.set()
        process_query_started.set()
        assert release_process_query.wait(2)
        return {"GPU-AAA": [{"pid": 1234, "memory_mb": 2048}]}

    def second_request():
        second_request_started.set()
        return monitor._get_gpu_metrics(detail=False)

    with (
        patch.object(monitor, "_query_gpu_snapshot", return_value=("0, GPU, GPU-AAA, 5, 1024, 24576\n", False)),
        patch.object(monitor, "_get_gpu_processes", side_effect=slow_processes),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(monitor._get_gpu_metrics, detail=False)
        try:
            assert process_query_started.wait(2)
            second = pool.submit(second_request)
            assert second_request_started.wait(2)
            duplicate_before_release = duplicate_query.wait(0.2)
        finally:
            release_process_query.set()
        first_result = first.result(timeout=2)
        assert second.result(timeout=2) == first_result
        assert first_result[0]["processes"][0]["pid"] == 1234

    assert duplicate_before_release is False
    assert query_count == 1


def test_unrecognized_gpu_process_response_is_not_a_successful_empty_list():
    with patch("pyruns.core.system_metrics.subprocess.check_output") as query:
        query.side_effect = [
            b"0, NVIDIA RTX 5090, GPU-AAA, 75, 8192, 24576\n",
            b"[Not Supported]\n",
        ]
        gpu = SystemMonitor().sample()["gpus"][0]
        assert "unrecognized" in gpu["processes_error"]


def test_gpu_process_query_has_separate_on_demand_timeout():
    with patch("pyruns.core.system_metrics.subprocess.check_output", return_value=b"") as query:
        monitor = SystemMonitor()
        monitor._get_gpu_processes()
        assert query.call_args.kwargs["timeout"] == 5
        monitor.sample(include_processes=False)
        assert query.call_args.kwargs["timeout"] == 1
