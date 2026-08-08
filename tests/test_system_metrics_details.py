from unittest.mock import MagicMock, patch

from pyruns.core.system_metrics import SystemMonitor


@patch("pyruns.core.system_metrics.psutil")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_metrics_skip_gpu_process_query_until_details_are_requested(mock_check_output, mock_psutil):
    mock_psutil.cpu_percent.return_value = 12.0
    mock_psutil.virtual_memory().percent = 34.0
    mock_check_output.return_value = (
        b"0, NVIDIA RTX 5090, GPU-AAA, 5.0, 1024.0, 24576.0\n"
    )

    metrics = SystemMonitor().sample(include_processes=False)

    assert mock_check_output.call_count == 1
    assert metrics["gpus"][0]["processes"] == []
    mock_psutil.Process.assert_not_called()


@patch("pyruns.core.system_metrics.psutil")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_metrics_keep_unavailable_process_memory_unknown(mock_check_output, mock_psutil):
    mock_psutil.cpu_percent.return_value = 12.0
    mock_psutil.virtual_memory().percent = 34.0
    mock_psutil.Process.return_value = MagicMock(username=MagicMock(return_value="researcher"))
    mock_check_output.side_effect = [
        b"0, NVIDIA RTX 5090, GPU-AAA, 5.0, 1024.0, 24576.0\n",
        b"GPU-AAA, 1234, python.exe, [N/A]\n",
    ]

    metrics = SystemMonitor().sample(include_processes=True)

    process = metrics["gpus"][0]["processes"][0]
    assert process["memory_mb"] is None
    assert process["user"] == "researcher"
