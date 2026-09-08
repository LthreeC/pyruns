import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
        **hidden_subprocess_kwargs(),
    }
    assert metrics["gpus"][0]["processes"] == []
    mock_psutil.Process.assert_not_called()


@patch("pyruns.core.system_metrics.psutil")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_metrics_keep_unavailable_process_memory_unknown(
    mock_check_output,
    mock_psutil,
):
    mock_psutil.cpu_percent.return_value = 12.0
    mock_psutil.virtual_memory().percent = 34.0
    mock_psutil.Process.return_value = MagicMock(
        username=MagicMock(return_value="researcher")
    )
    mock_check_output.side_effect = [
        b"0, NVIDIA RTX 5090, GPU-AAA, 5.0, 1024.0, 24576.0\n",
        b"GPU-AAA, 1234, python.exe, [N/A]\n",
    ]

    metrics = SystemMonitor().sample(include_processes=True)

    process = metrics["gpus"][0]["processes"][0]
    assert process["memory_mb"] is None
    assert process["user"] == "researcher"


@patch("pyruns.core.system_metrics.psutil")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_metrics_expose_bounded_os_process_details_once_per_pid(
    mock_check_output,
    mock_psutil,
):
    mock_psutil.cpu_percent.return_value = 12.0
    mock_psutil.virtual_memory().percent = 34.0
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
    mock_psutil.Process.return_value = os_process
    mock_check_output.side_effect = [
        (
            b"0, NVIDIA RTX 5090, GPU-AAA, 75, 8192, 24576\n"
            b"1, NVIDIA RTX 5090, GPU-BBB, 25, 4096, 24576\n"
        ),
        (
            b"GPU-AAA, 1234, /usr/bin/python, 4096\n"
            b"GPU-BBB, 1234, /usr/bin/python, 4096\n"
        ),
    ]

    gpus = SystemMonitor().sample(include_processes=True)["gpus"]

    process = gpus[0]["processes"][0]
    assert process["user"] == "researcher"
    assert process["process_name"] == "python"
    assert process["status"] == "sleeping"
    assert process["executable"] == "/usr/bin/python"
    assert "train.py" in process["command_line"]
    assert "--epochs" in process["command_line"]
    assert process["command_line_truncated"] is False
    assert process["working_directory"] == "/workspace"
    assert process["created_at"] == 1_725_000_000.0
    assert process["host_memory_mb"] == 1536.0
    assert process["host_memory_percent"] == 6.25
    assert process["thread_count"] == 12
    assert process["parent_pid"] == 1000
    assert gpus[1]["processes"][0]["process_name"] == "python"
    mock_psutil.Process.assert_called_once_with(1234)


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

    gpu = SystemMonitor().sample(include_processes=False)["gpus"][0]

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

    gpu = SystemMonitor().sample(include_processes=False)["gpus"][0]

    assert gpu["name"] == "NVIDIA RTX 4090"
    assert gpu["temperature_c"] is None
    assert gpu["driver_version"] == ""
    assert mock_check_output.call_args_list[1].args[0][1] == (
        f"--query-gpu={SystemMonitor._GPU_SUMMARY_FIELDS}"
    )


@patch("pyruns.core.system_metrics.time.monotonic")
@patch("pyruns.core.system_metrics.psutil")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_detail_request_keeps_process_shape_when_gpu_refresh_fails(
    mock_check_output,
    mock_psutil,
    mock_monotonic,
):
    mock_psutil.cpu_percent.return_value = 12.0
    mock_psutil.virtual_memory().percent = 34.0
    mock_monotonic.side_effect = [10.0, 12.0]
    mock_check_output.side_effect = [
        b"0, NVIDIA RTX 5090, GPU-AAA, 5.0, 1024.0, 24576.0\n",
        OSError("nvidia-smi temporarily unavailable"),
    ]
    monitor = SystemMonitor(gpu_ttl_sec=1.5)

    summary_gpu = monitor.sample(include_processes=False)["gpus"][0]
    detail_gpu = monitor.sample(include_processes=True)["gpus"][0]

    assert summary_gpu["processes"] == []
    assert detail_gpu["uuid"] == "GPU-AAA"
    assert detail_gpu["processes"] == []
