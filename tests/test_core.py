"""Core configuration, system metrics, task registries, and workspaces."""

import gc
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest
import psutil
from unittest.mock import patch, MagicMock

import pyruns.core.executor as executor
import pyruns.core.task_manager as task_manager_module
from pyruns._config import (
    CONFIG_DEFAULT_FILENAME,
    CONFIG_FILENAME,
    ERROR_LOG_FILENAME,
    POWERSHELL_CONFIG_FILENAME,
    DEFAULT_ROOT_NAME,
    RUN_LOGS_DIR,
    SCRIPT_INFO_FILENAME,
    SHELL_CONFIG_FILENAME,
    SHELL_WORKSPACE_NAME,
    TASKS_DIR,
    TASK_INFO_FILENAME,
    TRASH_DIR,
    TASK_KIND_CONFIG,
    TASK_KIND_SHELL,
    MAX_CONFIG_FILE_BYTES,
    WORKSPACE_KIND_SHELL,
)
from omegaconf import DictConfig, ListConfig, OmegaConf

from pyruns.core.config_manager import ConfigManager
from pyruns.core.gpu_scheduler import GpuAssignment, GpuDecision, GpuDevice, GpuResourceScheduler, GpuSchedulerConfig
from pyruns.core.system_metrics import SystemMonitor
from pyruns.core.task_generator import TaskGenerator
from pyruns.core.task_manager import TaskManager, TaskStateConflict
from pyruns.launcher import (
    bootstrap_shell_workspace,
    bootstrap_workspace,
    list_script_candidates,
    shell_workspace_root_for_run_root,
    workspace_root_for_script,
)
from pyruns.utils.info_io import (
    MAX_RUN_HISTORY_SLOTS,
    ensure_run_slot,
    load_task_info,
    normalize_run_history,
    run_slot_count,
    save_task_info,
    update_task_info,
)
from pyruns.utils.config_utils import save_yaml
from pyruns.utils.shell_runtime import get_shell_config_filename_for_workspace, get_shell_runtime_for_workspace


class _StaticGpuProvider:
    def __init__(self, devices):
        self.devices = devices
        self.calls = 0

    def sample(self):
        self.calls += 1
        return list(self.devices)


def _make_task_manager(tasks_dir: Path, *, lazy_scan=False, **kwargs) -> TaskManager:
    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        return TaskManager(
            tasks_dir=str(tasks_dir),
            lazy_scan=lazy_scan,
            **kwargs,
        )


def _mark_task_owned_by_manager(
    manager: TaskManager,
    task_name: str,
    task_dir: Path,
    *,
    pids: list[int] | None = None,
    counts_for_batch: bool = True,
) -> None:
    def _apply(info):
        info["status"] = "running"
        info["run_index"] = 1
        info["pids"] = list(pids or [12345])
        info["pid_create_times"] = [1000.0 for _pid in info["pids"]]
        info["runner_id"] = manager.runner_id
        info["runner_host"] = manager.runner_host
        info["lease_heartbeat"] = time.time()
        info["lease_until"] = time.time() + 60

    updated = update_task_info(str(task_dir), _apply)
    with manager._lock:
        current = manager._tasks_by_name[task_name]
        manager._apply_info_to_task(current, updated)
        manager._mark_running_locked(task_name, counts_for_batch=counts_for_batch)
        manager._recompute_processing_flag_locked()


def _write_running_config_task(task_dir: Path) -> None:
    """Seed the common running task used by cancellation and deletion tests."""
    save_task_info(
        str(task_dir),
        {
            "name": "runner",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [12345],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})


def test_omegaconf_nested_access_and_container_export():
    data = {
        "lr": 0.01,
        "optimizer": {
            "name": "adam",
            "beta": 0.9
        },
        "layers": [64, 128, {"dropout": 0.5}],
        "label": "train",
        "_private": "hidden",
    }
    node = OmegaConf.create(data)

    assert node.lr == 0.01
    assert node.optimizer.name == "adam"
    assert node.optimizer.beta == 0.9
    assert len(node.layers) == 3
    assert node.layers[0] == 64
    assert node.layers[2].dropout == 0.5
    assert node["_private"] == "hidden"
    d = OmegaConf.to_container(node, resolve=False)
    assert "lr" in d
    assert "optimizer" in d
    assert isinstance(d["optimizer"], dict)
    assert d["optimizer"]["name"] == "adam"
    assert isinstance(d["layers"], list)
    assert isinstance(d["layers"][2], dict)
    assert d["layers"][2]["dropout"] == 0.5
    assert d["_private"] == "hidden"
    assert isinstance(node, DictConfig)
    assert isinstance(node.optimizer, DictConfig)
    assert isinstance(node.layers, ListConfig)


def test_config_manager_rejects_unloaded_missing_unsupported_and_invalid(
    tmp_path,
    monkeypatch,
):
    cm = ConfigManager()
    with pytest.raises(RuntimeError, match="not loaded"):
        cm.load()

    cm = ConfigManager()
    with pytest.raises(FileNotFoundError):
        cm.read("does_not_exist_at_all.yaml")

    p = tmp_path / "cfg.txt"
    p.write_text("Hello", encoding="utf-8")
    cm = ConfigManager()
    with pytest.raises(RuntimeError, match="Unsupported format"):
        cm.read(str(p))

    class MockLogger:
        def __init__(self):
            self.logs = []

        def info(self, msg, *args):
            self.logs.append(("INFO", msg % args))

        def error(self, msg, *args):
            self.logs.append(("ERROR", msg % args))

    logger = MockLogger()
    monkeypatch.setattr("pyruns.core.config_manager.logger", logger)
    p = tmp_path / "bad.yaml"
    p.write_text("a: \n  - b:\n c: [invalid yaml", encoding="utf-8")
    cm = ConfigManager()
    with pytest.raises(RuntimeError, match="Failed to parse config"):
        cm.read(str(p))
    assert any("Failed to parse config" in msg for level, msg in logger.logs if level == "ERROR")


def test_config_manager_reads_yaml_json_and_list(tmp_path):
    yaml_path = tmp_path / "cfg.yaml"
    yaml_path.write_text("a: 1\nb: 2", encoding="utf-8")
    yaml_manager = ConfigManager()
    yaml_manager.read(str(yaml_path))
    yaml_node = yaml_manager.load()
    assert yaml_node.a == 1
    assert yaml_node.b == 2

    json_path = tmp_path / "cfg.json"
    json_path.write_text('{"a": 1, "b": {"c": 3}}', encoding="utf-8")
    json_manager = ConfigManager()
    json_manager.read(str(json_path))
    json_node = json_manager.load()
    assert json_node.a == 1
    assert json_node.b.c == 3

    list_path = tmp_path / "list.yaml"
    list_path.write_text("- a: 1\n- b: 2", encoding="utf-8")
    list_manager = ConfigManager()
    list_manager.read(str(list_path))
    nodes = list_manager.load()
    assert isinstance(nodes, ListConfig)
    assert len(nodes) == 2
    assert nodes[0].a == 1
    assert nodes[1].b == 2


def test_config_manager_uses_omegaconf_interpolation_and_pyruns_scalars(tmp_path):
    path = tmp_path / "advanced.yaml"
    path.write_text(
        "base: /tmp\n"
        "output: ${base}/results\n"
        "range: 30:40:1\n"
        "scientific: 5e-3\n",
        encoding="utf-8",
    )

    manager = ConfigManager()
    manager.read(str(path))
    config = manager.load()

    assert isinstance(config, DictConfig)
    assert config.output == "/tmp/results"
    assert config.range == "30:40:1"
    assert config.scientific == 0.005
    assert OmegaConf.to_container(config, resolve=False)["output"] == "${base}/results"


def test_config_manager_rejects_oversized_config(tmp_path):
    path = tmp_path / "oversized.yaml"
    path.write_bytes(b"value: " + b"x" * (MAX_CONFIG_FILE_BYTES + 1))

    with pytest.raises(RuntimeError, match="too large"):
        ConfigManager().read(str(path))


#  SystemMonitor

@patch("pyruns.core.system_metrics.psutil")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_system_monitor_sample(mock_subprocess, mock_psutil):
    # Setup CPU/RAM mocks
    mock_psutil.cpu_percent.return_value = 25.5
    mock_mem = MagicMock()
    mock_mem.percent = 60.0
    mock_psutil.virtual_memory.return_value = mock_mem
    # Setup GPU + process mocks
    mock_subprocess.side_effect = [
        (
            b"0, NVIDIA RTX 4090, GPU-AAA, 45.0, 4000.0, 8000.0\n"
            b"1, NVIDIA RTX 4080, GPU-BBB, 90.0, 8000.0, 8000.0\n"
        ),
        (
            b"GPU-AAA, 1234, python.exe, 2048\n"
            b"GPU-AAA, 9999, tensorboard.exe, 256\n"
            b"GPU-BBB, 5678, train.py, 4096\n"
        ),
    ]
    
    monitor = SystemMonitor()
    metrics = monitor.sample()
    
    # Assert CPU/RAM
    assert metrics["cpu_percent"] == 25.5
    assert metrics["mem_percent"] == 60.0
    
    # Assert GPU
    gpus = metrics["gpus"]
    assert len(gpus) == 2
    assert gpus[0]["id"] == 0
    assert gpus[0]["index"] == 0
    assert gpus[0]["name"] == "NVIDIA RTX 4090"
    assert gpus[0]["uuid"] == "GPU-AAA"
    assert gpus[0]["util"] == 45.0
    assert gpus[0]["mem_used"] == 4000.0
    assert gpus[0]["mem_total"] == 8000.0
    assert [proc["pid"] for proc in gpus[0]["processes"]] == [1234, 9999]
    
    assert gpus[1]["index"] == 1
    assert gpus[1]["name"] == "NVIDIA RTX 4080"
    assert gpus[1]["util"] == 90.0
    assert gpus[1]["processes"][0]["name"] == "train.py"
    assert mock_psutil.Process.call_count == 3

    assert monitor._gpu_cache == [
        {key: value for key, value in gpu.items() if key != "processes"}
        for gpu in gpus
    ]


@patch("pyruns.core.system_metrics.psutil")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_system_monitor_gpu_error(mock_subprocess, mock_psutil):
    mock_psutil.cpu_percent.return_value = 10.0
    mock_psutil.virtual_memory().percent = 20.0
    
    # Setup GPU mock to fail
    mock_subprocess.side_effect = Exception("nvidia-smi failed")
    
    monitor = SystemMonitor()
    # Pre-populate cache to test fallback
    cached_gpus = [{"index": 0, "util": 10.0, "mem_used": 1000.0, "mem_total": 8000.0}]
    monitor._gpu_cache = cached_gpus
    
    metrics = monitor.sample()

    # Error should return a well-formed copy of the cached device snapshot.
    assert metrics["gpus"] == [{
        **cached_gpus[0], "processes": [],
        "processes_error": "Could not query NVIDIA processes: nvidia-smi failed",
    }]


@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_system_monitor_gpu_empty(mock_subprocess):
    # Setup GPU to return empty (e.g. no GPUs or driver not loaded properly but command succeeds)
    mock_subprocess.side_effect = [b"   \n\n\n", b""]
    
    monitor = SystemMonitor()
    gpus = monitor._get_gpu_metrics()
    assert gpus == []


@patch("pyruns.core.system_metrics.time.monotonic")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_system_monitor_reuses_empty_gpu_cache_until_ttl_expires(mock_subprocess, mock_monotonic):
    mock_monotonic.return_value = 10.0
    mock_subprocess.side_effect = [
        b"   \n\n\n",
        b"",
        b"0, NVIDIA RTX 4090, GPU-AAA, 1.0, 1000.0, 8000.0\n",
        b"",
    ]

    monitor = SystemMonitor()

    assert monitor._get_gpu_metrics() == []
    assert monitor._gpu_cache_valid is True
    mock_monotonic.return_value = 10.5
    assert monitor._get_gpu_metrics() == []
    assert mock_subprocess.call_count == 2

    mock_monotonic.return_value = 12.0
    gpus = monitor._get_gpu_metrics()

    assert len(gpus) == 1
    assert gpus[0]["uuid"] == "GPU-AAA"
    assert mock_subprocess.call_count == 4


def test_system_monitor_coalesces_concurrent_gpu_queries():
    monitor = SystemMonitor(gpu_ttl_sec=60)
    query_started = threading.Event()
    second_started = threading.Event()
    duplicate_query = threading.Event()
    release_query = threading.Event()
    query_count = 0
    count_lock = threading.Lock()

    def query_snapshot(*, detail):
        nonlocal query_count
        with count_lock:
            query_count += 1
            if query_count > 1:
                duplicate_query.set()
        query_started.set()
        assert release_query.wait(2)
        return "0, GPU, GPU-1, 1, 100, 1000\n", False

    def second_request():
        second_started.set()
        return monitor._get_gpu_metrics(include_processes=False, detail=False)

    with patch.object(monitor, "_query_gpu_snapshot", side_effect=query_snapshot):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(monitor._get_gpu_metrics, include_processes=False, detail=False)
            assert query_started.wait(2)
            second = pool.submit(second_request)
            assert second_started.wait(2)
            duplicate_before_release = duplicate_query.wait(0.2)
            release_query.set()
            assert first.result(timeout=2) == second.result(timeout=2)

    assert duplicate_before_release is False
    assert query_count == 1


@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_system_monitor_gpu_process_query_failure_still_returns_gpu_summary(mock_subprocess):
    mock_subprocess.side_effect = [
        b"0, NVIDIA RTX 4090, GPU-AAA, 45.0, 4000.0, 8000.0\n",
        Exception("process query failed"),
    ]

    monitor = SystemMonitor()
    gpus = monitor._get_gpu_metrics()

    assert len(gpus) == 1
    assert gpus[0]["name"] == "NVIDIA RTX 4090"
    assert gpus[0]["processes"] == []


@patch("pyruns.core.system_metrics.subprocess.check_output")
@patch("pyruns.core.system_metrics.psutil.Process")
def test_system_monitor_gpu_process_summary_reads_only_owner(mock_process, mock_subprocess):
    mock_subprocess.return_value = b"GPU-AAA, 1234, python.exe, 2048\n"
    mock_process.return_value.username.return_value = "researcher"

    monitor = SystemMonitor()
    processes = monitor._get_gpu_processes()

    assert processes["GPU-AAA"][0] == {
        "pid": 1234,
        "name": "python.exe",
        "user": "researcher",
        "memory_mb": 2048.0,
    }
    assert [call[0] for call in mock_process.return_value.method_calls] == ["username"]


@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_system_monitor_gpu_csv_parser_handles_quoted_names(mock_subprocess):
    mock_subprocess.side_effect = [
        b'0, "NVIDIA, RTX 4090", GPU-AAA, 45.0, 4000.0, 8000.0\n',
        b'GPU-AAA, 1234, "python, train.py", 2048\n',
    ]

    monitor = SystemMonitor()
    gpus = monitor._get_gpu_metrics()

    assert gpus[0]["name"] == "NVIDIA, RTX 4090"
    assert gpus[0]["processes"][0]["name"] == "python, train.py"


@patch("pyruns.core.system_metrics.time.monotonic")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_system_monitor_retries_after_gpu_disable_cooldown(mock_subprocess, mock_monotonic):
    mock_monotonic.return_value = 0.0
    mock_subprocess.side_effect = [
        Exception("nvidia-smi failed"),
        Exception("nvidia-smi failed"),
        Exception("nvidia-smi failed"),
        b"0, NVIDIA RTX 4090, GPU-AAA, 45.0, 4000.0, 8000.0\n",
        b"",
    ]

    monitor = SystemMonitor()

    assert monitor._get_gpu_metrics() == []
    mock_monotonic.return_value = 1.0
    assert monitor._get_gpu_metrics() == []
    mock_monotonic.return_value = 2.0
    assert monitor._get_gpu_metrics() == []
    assert monitor._gpu_available is False
    assert mock_subprocess.call_count == 3

    mock_monotonic.return_value = 20.0
    assert monitor._get_gpu_metrics() == []
    assert mock_subprocess.call_count == 3

    mock_monotonic.return_value = 40.0
    gpus = monitor._get_gpu_metrics()
    assert len(gpus) == 1
    assert gpus[0]["uuid"] == "GPU-AAA"
    assert monitor._gpu_available is True


#  Executor


@patch("pyruns.utils.shell_runtime.get_follow_shell_runtime")
def test_shell_runtime_follow_mode_ignores_shell_executable_setting(mock_follow_shell, tmp_path):
    workspace = tmp_path / "_pyruns_" / "main"
    workspace.mkdir(parents=True)
    settings_path = workspace.parent / "_pyruns_settings.yaml"
    settings_path.write_text("shell_mode: follow\nshell_executable: bash.exe\n", encoding="utf-8")
    mock_follow_shell.return_value = {
        "mode": "follow",
        "source": "follow_terminal",
        "terminal_kind": "powershell",
        "display_name": "PowerShell",
        "executable": r"C:\Program Files\PowerShell\7\pwsh.exe",
        "available": True,
    }

    runtime = get_shell_runtime_for_workspace(str(workspace))

    assert runtime["mode"] == "follow"
    assert runtime["terminal_kind"] == "powershell"
    assert runtime["executable"] == r"C:\Program Files\PowerShell\7\pwsh.exe"


def test_shell_runtime_custom_mode_uses_explicit_shell_executable(tmp_path):
    workspace = tmp_path / "_pyruns_" / "main"
    workspace.mkdir(parents=True)
    settings_path = workspace.parent / "_pyruns_settings.yaml"
    settings_path.write_text(
        "shell_mode: custom\nshell_executable: /custom/shell\n",
        encoding="utf-8",
    )

    runtime = get_shell_runtime_for_workspace(str(workspace))

    assert runtime["mode"] == "custom"
    assert runtime["source"] == "custom_shell"
    assert runtime["executable"] == "/custom/shell"


def test_shell_runtime_custom_mode_marks_known_shell_unavailable_when_it_cannot_start(tmp_path):
    workspace = tmp_path / "_pyruns_" / "main"
    workspace.mkdir(parents=True)
    fake_bash = tmp_path / "bash.exe"
    fake_bash.write_text("not a real shell", encoding="utf-8")
    settings_path = workspace.parent / "_pyruns_settings.yaml"
    settings_path.write_text(
        "shell_mode: custom\n"
        f"shell_executable: {json.dumps(str(fake_bash))}\n",
        encoding="utf-8",
    )

    runtime = get_shell_runtime_for_workspace(str(workspace))

    assert runtime["terminal_kind"] == "bash"
    assert runtime["executable"] == str(fake_bash)
    assert runtime["available"] is False


def test_shell_runtime_custom_mode_marks_unknown_shell_unavailable(tmp_path):
    workspace = tmp_path / "_pyruns_" / "main"
    workspace.mkdir(parents=True)
    fake_shell = tmp_path / "not-a-shell.bin"
    fake_shell.write_text("not a real shell", encoding="utf-8")
    settings_path = workspace.parent / "_pyruns_settings.yaml"
    settings_path.write_text(
        "shell_mode: custom\n"
        f"shell_executable: {json.dumps(str(fake_shell))}\n",
        encoding="utf-8",
    )

    runtime = get_shell_runtime_for_workspace(str(workspace))

    assert runtime["terminal_kind"] == "unknown"
    assert runtime["display_name"] == "Custom shell"
    assert runtime["executable"] == str(fake_shell)
    assert runtime["available"] is False


def test_shell_runtime_follow_mode_probes_detected_shell_availability(tmp_path):
    workspace = tmp_path / "_pyruns_" / "main"
    workspace.mkdir(parents=True)
    fake_bash = tmp_path / "follow-bash.exe"
    fake_bash.write_text("not a real shell", encoding="utf-8")

    with patch("pyruns.utils.shell_runtime.get_follow_shell_runtime") as mock_runtime:
        mock_runtime.return_value = {
            "source": "follow_terminal",
            "terminal_kind": "bash",
            "display_name": "Bash",
            "executable": str(fake_bash),
            "available": True,
        }

        runtime = get_shell_runtime_for_workspace(str(workspace))

    assert runtime["mode"] == "follow"
    assert runtime["terminal_kind"] == "bash"
    assert runtime["available"] is False


def test_shell_runtime_config_filename_tracks_custom_shell_kind(tmp_path):
    workspace = tmp_path / "_pyruns_" / "main"
    workspace.mkdir(parents=True)
    settings_path = workspace.parent / "_pyruns_settings.yaml"

    settings_path.write_text("shell_mode: custom\nshell_executable: sh\n", encoding="utf-8")
    assert get_shell_config_filename_for_workspace(str(workspace)) == SHELL_CONFIG_FILENAME

    settings_path.write_text("shell_mode: custom\nshell_executable: pwsh.exe\n", encoding="utf-8")
    assert get_shell_config_filename_for_workspace(str(workspace)) == POWERSHELL_CONFIG_FILENAME


def test_shell_workspace_root_resolves_project_and_script_roots(tmp_path):
    project_root = tmp_path / DEFAULT_ROOT_NAME
    project_root.mkdir(parents=True)
    assert shell_workspace_root_for_run_root(str(project_root)) == str(
        project_root / SHELL_WORKSPACE_NAME
    ).replace("\\", "/")

    script_root = project_root / "main"
    script_root.mkdir(parents=True)
    assert shell_workspace_root_for_run_root(str(script_root)) == str(
        project_root / SHELL_WORKSPACE_NAME
    ).replace("\\", "/")


def test_shell_named_python_script_uses_reserved_safe_workspace_dir(tmp_path):
    script_path = tmp_path / "_shell_.py"
    script_path.write_text(
        "\n".join(
            [
                "import argparse",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--epochs', type=int, default=3)",
                "parser.parse_args()",
                "",
            ]
        ),
        encoding="utf-8",
    )

    expected_workspace = str(tmp_path / DEFAULT_ROOT_NAME / f"py{SHELL_WORKSPACE_NAME}").replace("\\", "/")

    assert workspace_root_for_script(str(script_path)) == expected_workspace
    workspace = bootstrap_workspace(str(script_path))
    info = json.loads(Path(workspace, SCRIPT_INFO_FILENAME).read_text(encoding="utf-8"))

    assert workspace == expected_workspace
    assert info["workspace_kind"] == "script"
    assert info["script_name"] == "_shell_"
    assert shell_workspace_root_for_run_root(workspace) == str(
        tmp_path / DEFAULT_ROOT_NAME / SHELL_WORKSPACE_NAME
    ).replace("\\", "/")
    assert any(
        item["script_name"] == "_shell_" and item["workspace_path"] == expected_workspace
        for item in list_script_candidates(str(tmp_path))
    )


@pytest.mark.parametrize("filename", ["shell.py", "SHELL.py"])
def test_shell_workspace_selector_is_reserved_from_python_script_names(tmp_path, filename):
    script_path = tmp_path / filename
    script_path.write_text("print('reserved')\n", encoding="utf-8")

    with pytest.raises(ValueError, match="reserved for the shell workspace selector"):
        workspace_root_for_script(str(script_path))
    with pytest.raises(ValueError, match="reserved for the shell workspace selector"):
        bootstrap_workspace(str(script_path))

    assert not (tmp_path / DEFAULT_ROOT_NAME).exists()


def test_bootstrap_shell_workspace_records_project_root(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    shell_root = bootstrap_shell_workspace(str(project_root / DEFAULT_ROOT_NAME))
    info = json.loads(Path(shell_root, SCRIPT_INFO_FILENAME).read_text(encoding="utf-8"))

    assert shell_root == str(project_root / DEFAULT_ROOT_NAME / SHELL_WORKSPACE_NAME).replace("\\", "/")
    assert info["project_root"] == str(project_root).replace("\\", "/")


def test_spawn_captured_process_hides_windows_console(monkeypatch, tmp_path):
    import pyruns.core.executor as executor

    sentinel = object()
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(executor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        executor,
        "spawn_terminal_process",
        MagicMock(side_effect=Exception("ConPTY unavailable")),
    )
    monkeypatch.setattr(executor, "_is_windows", lambda: True)
    monkeypatch.setattr(
        executor.subprocess,
        "CREATE_NO_WINDOW",
        0x08000000,
        raising=False,
    )

    result = executor._spawn_captured_process(
        ["powershell", "-File", "task.ps1"],
        workdir=str(tmp_path),
        env={"PATH": "test"},
        preserve_terminal_output=True,
    )

    assert result is sentinel
    assert captured["command"] == ["powershell", "-File", "task.ps1"]
    assert captured["kwargs"]["creationflags"] == 0x08000000
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    assert captured["kwargs"]["stdout"] is subprocess.PIPE
    assert captured["kwargs"]["stderr"] is subprocess.STDOUT
    assert captured["kwargs"]["shell"] is False


def test_terminal_output_filter_preserves_sgr_and_removes_screen_controls():
    from pyruns.utils.terminal_capture import _SgrOutputFilter

    output_filter = _SgrOutputFilter()
    rendered = b"".join(
        [
            output_filter.feed(b"\x1b[?9001h\x1b[?25l\x1b[2"),
            output_filter.feed(b"J\x1b[m\x1b[38;5;9mred"),
            output_filter.feed(
                b"\x1b]0;PowerShell\x07\x1b[?25h\x1b[m\r\n"
            ),
            output_filter.finish(),
        ]
    )

    assert rendered == b"\x1b[m\x1b[38;5;9mred\x1b[m\r\n"
    assert b"\x1b[2J" not in rendered
    assert b"PowerShell" not in rendered

    unterminated_color = _SgrOutputFilter()
    rendered = b"".join(
        [
            unterminated_color.feed(b"\x1b[38;5;9merror\r\n"),
            unterminated_color.finish(),
        ]
    )
    assert rendered == b"\x1b[38;5;9merror\r\n\x1b[0m"
    assert unterminated_color.finish() == b""


def test_windows_terminal_capture_forces_native_conpty(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from pyruns.utils import terminal_capture

    fake_process = MagicMock()
    fake_process.pid = 1234
    monkeypatch.setenv("PYWINPTY_BACKEND", "1")

    def native_spawn(*args, backend=None, **kwargs):
        # Match pywinpty's fallback: numeric zero would select legacy WinPTY.
        assert int(backend or os.environ.get("PYWINPTY_BACKEND")) == 0
        return fake_process

    spawn = MagicMock(side_effect=native_spawn)
    monkeypatch.setitem(
        sys.modules,
        "winpty",
        SimpleNamespace(
            Backend=SimpleNamespace(ConPTY=0),
            PtyProcess=SimpleNamespace(spawn=spawn),
        ),
    )

    adapter = terminal_capture._spawn_windows_conpty(
        ["powershell", "-File", "task.ps1"],
        cwd=str(tmp_path),
        env={"PATH": "test", "LINES": "60"},
    )

    assert adapter.pid == 1234
    assert spawn.call_args.kwargs["backend"] == "0"
    assert spawn.call_args.kwargs["dimensions"] == (60, 160)
    assert os.environ["PYWINPTY_BACKEND"] == "1"
    assert spawn.call_args.kwargs["env"]["TERM"] == "xterm-256color"
    assert spawn.call_args.kwargs["env"]["COLORTERM"] == "truecolor"
    fake_process.read.side_effect = [
        "\x1b[K\r\n" * 59 + "\x1b[K\x1b[H", "next\r\n",
    ]
    assert adapter.stdout.read1(8192) == b"next\r\n"


@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX PTY")
def test_posix_terminal_capture_preserves_command_colors(tmp_path):
    from pyruns.utils.terminal_capture import _spawn_posix_pty

    process = _spawn_posix_pty(
        ["sh", "-c", "printf '\\033[31mred\\033[0m\\n'"],
        cwd=str(tmp_path),
        env=os.environ.copy(),
    )
    chunks = []
    while True:
        chunk = process.stdout.read1(4096)
        if not chunk:
            break
        chunks.append(chunk)
    assert process.wait(timeout=5) == 0
    process.close_output()

    assert b"\x1b[31mred\x1b[0m" in b"".join(chunks)


def test_build_shell_command_requires_existing_script(tmp_path):
    import pyruns.core.executor as executor

    with pytest.raises(FileNotFoundError):
        executor._build_shell_command(str(tmp_path), SHELL_CONFIG_FILENAME)


def test_build_run_source_state_records_file_hashes_without_config_hash(tmp_path, monkeypatch):
    from pyruns.core import executor

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    script = tmp_path / "train.py"
    script.write_text("print('train')\n", encoding="utf-8")
    config = task_dir / CONFIG_FILENAME
    config.write_text("lr: 0.01\n", encoding="utf-8")
    monkeypatch.setattr(
        executor,
        "_build_git_source_state",
        lambda cwd: "git none | unknown",
    )

    state = executor._build_run_source_state(
        task_dir=str(task_dir),
        script_path=str(script),
        workdir=str(tmp_path),
    )

    assert "git none | unknown" in state
    assert "| script " in state
    assert "config" not in state


def test_build_git_source_state_reports_clean_dirty_and_unknown(monkeypatch):
    from pyruns.core import executor

    status_output = b""

    def fake_git_bytes(cwd, args, **kwargs):
        if args == ["rev-parse", "--show-toplevel"]:
            return b"/repo\n"
        if args == ["rev-parse", "--short=12", "HEAD"]:
            return b"abc123def456\n"
        if args == ["status", "--porcelain=v1", "-z", "--untracked-files=normal"]:
            return status_output
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr(executor, "_git_bytes", fake_git_bytes)

    assert executor._build_git_source_state("/repo") == "git abc123def456 | clean"

    status_output = b" M train.py\0?? scratch.py\0"
    assert executor._build_git_source_state("/repo") == "git abc123def456 | dirty"

    status_output = None
    assert executor._build_git_source_state("/repo") == "git abc123def456 | unknown"


def test_build_git_source_state_reports_unknown_without_git_root(monkeypatch):
    from pyruns.core import executor

    monkeypatch.setattr(executor, "_git_bytes", lambda cwd, args, **kwargs: None)

    assert executor._build_git_source_state("/not-a-repo") == "git none | unknown"


def test_git_bytes_disables_optional_git_locks(monkeypatch):
    from pyruns.core import executor

    captured = {}

    class Result:
        returncode = 0
        stdout = b"ok"

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        captured["kwargs"] = kwargs
        return Result()

    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")
    monkeypatch.setattr(executor.subprocess, "run", fake_run)

    assert executor._git_bytes("/repo", ["status", "--porcelain=v1"]) == b"ok"
    assert captured["command"] == ["git", "status", "--porcelain=v1"]
    assert captured["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    for key, value in executor.hidden_subprocess_kwargs().items():
        assert captured["kwargs"][key] == value
    assert os.environ["GIT_OPTIONAL_LOCKS"] == "1"



def test_terminate_started_process_uses_owned_popen_when_create_time_is_unavailable(monkeypatch):
    mock_proc = MagicMock()
    mock_proc.pid = 9875
    mock_proc.poll.side_effect = [None, 0]
    killed = []
    monkeypatch.setattr(
        executor,
        "kill_process",
        lambda pid, expected_create_time=None: killed.append((pid, expected_create_time)) or True,
    )

    terminated = executor._terminate_started_process(
        mock_proc,
        expected_create_time=None,
        task_name="OwnedProcessTask",
        run_index=1,
    )

    assert terminated is True
    assert killed == [(9875, None)]
    mock_proc.wait.assert_called_once_with(timeout=1)


def test_task_manager_uses_explicit_runner_token(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    manager = _make_task_manager(tasks_dir, runner_token="submission-token")

    assert manager.runner_id.rsplit(":", 1)[-1] == "submission-token"
    manager.shutdown()

def test_task_manager_start_batch_tasks_uses_available_slots_immediately(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    tasks = [generator.create_task(f"task-{idx}", {"value": idx}) for idx in range(5)]

    manager = _make_task_manager(tasks_dir)

    submitted: list[tuple[str, int, bool]] = []

    def fake_submit(target, run_index, *, independent):
        submitted.append((target["name"], run_index, independent))

    monkeypatch.setattr(manager, "_submit_task", fake_submit)

    manager.start_batch_tasks([task["name"] for task in tasks], max_workers=4)

    statuses = {task["name"]: task["status"] for task in manager.list_tasks()}
    assert sum(1 for status in statuses.values() if status == "running") == 4
    assert sum(1 for status in statuses.values() if status == "queued") == 1
    assert len(submitted) == 4

    for task in tasks[:4]:
        info = json.loads((Path(task["dir"]) / TASK_INFO_FILENAME).read_text(encoding="utf-8"))
        assert info["status"] == "running"

    queued_info = json.loads((Path(tasks[4]["dir"]) / TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    assert queued_info["status"] == "queued"
    assert queued_info["run_index"] == 0
    assert "_queued_run_index" not in queued_info


def test_task_manager_sync_status_does_not_revive_cancelled_queued_task(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("race-cancel", {"value": 1})

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        current = manager._tasks_by_name[task["name"]]
        current["status"] = "queued"
    update_task_info(task["dir"], lambda info: info.update({"status": "queued"}))

    assert manager.cancel_task(task["name"]) is True
    assert manager._sync_status_to_disk(
        task["name"],
        "queued",
        run_index=1,
        expected_statuses={"pending"},
    ) is False

    info = load_task_info(task["dir"])
    assert info["status"] == "cancelled"
    assert manager.get_task(task["name"])["status"] == "cancelled"


def test_task_manager_submit_after_delete_does_not_recreate_or_execute(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("delete-race", {"value": 1})
    update_task_info(task["dir"], lambda info: info.update({"status": "queued"}))

    manager = _make_task_manager(tasks_dir)

    picked, run_index = manager._pick_queued_task()
    assert picked is not None
    assert picked["name"] == task["name"]

    submitted: list[str] = []

    class CapturingExecutor:
        def __init__(self, max_workers=None):
            self.max_workers = max_workers

        def submit(self, *args, **kwargs):
            submitted.append(args[2])
            return Future()

        def shutdown(self, **kwargs):
            pass

    monkeypatch.setattr(task_manager_module, "ThreadPoolExecutor", CapturingExecutor)

    assert manager.delete_tasks([task["name"]]) == [task["name"]]
    assert not Path(task["dir"]).exists()

    manager._submit_task(picked, run_index, independent=False)

    assert submitted == []
    assert not Path(task["dir"]).exists()
    assert manager.get_task(task["name"]) is None


@pytest.mark.parametrize(
    ("process_live", "expected_status"),
    [(True, "running"), (False, "failed")],
)
def test_task_manager_expired_local_lease_uses_process_identity(
    tmp_path,
    monkeypatch,
    process_live,
    expected_status,
):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "expired"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "expired",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [12345],
            "pid_create_times": [1000.0],
            "records": [],
            "tracks": [],
            "runner_id": "old-runner",
            "runner_host": socket.gethostname().lower(),
            "lease_until": time.time() - 60,
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    killed: list[int] = []
    monkeypatch.setattr(
        "pyruns.core.task_manager.process_identity_matches",
        lambda pid, created_at: process_live and pid == 12345 and created_at == 1000.0,
    )
    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda pid, expected_create_time=None: killed.append(pid) or True,
    )

    manager = _make_task_manager(tasks_dir)

    assert manager.get_task("expired")["status"] == expected_status
    assert manager.cancel_task("expired") is False
    assert killed == []


def test_task_manager_does_not_fail_runner_that_renews_during_stale_reconciliation(
    tmp_path,
    monkeypatch,
):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "renewed"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "renewed",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [12345],
            "records": [],
            "tracks": [],
            "runner_id": "other-host:123:abcdef",
            "runner_host": socket.gethostname().lower(),
            "lease_until": time.time() - 60,
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda _pid: False)

    manager = _make_task_manager(tasks_dir, lazy_scan=None)

    stale_info = load_task_info(str(task_dir))
    mark_failed = manager._mark_failed_on_disk

    def renew_then_mark(task, **kwargs):
        update_task_info(
            str(task_dir),
            lambda info: info.update({"lease_until": time.time() + 60}),
        )
        return mark_failed(task, **kwargs)

    monkeypatch.setattr(manager, "_mark_failed_on_disk", renew_then_mark)

    updated, changed = manager._fail_unowned_running_info_if_needed(
        "renewed",
        str(task_dir),
        stale_info,
    )

    assert changed is False
    assert updated["status"] == "running"
    assert updated["lease_until"] > time.time()
    assert load_task_info(str(task_dir))["status"] == "running"


def test_task_manager_start_task_now_skips_active_task(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    task = generator.create_task("runner", {"value": 1})

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        active = manager._tasks_by_name[task["name"]]
        active["status"] = "running"
        active["run_index"] = 1
        manager._mark_running_locked(task["name"], counts_for_batch=True)
        manager._recompute_processing_flag_locked()

    submitted: list[str] = []
    monkeypatch.setattr(
        manager,
        "_submit_task",
        lambda target, run_index, *, independent: submitted.append(target["name"]),
    )

    manager.start_task_now(task["name"])

    assert submitted == []
    refreshed = manager.get_task(task["name"])
    assert refreshed["status"] == "running"
    assert refreshed["run_index"] == 1


def test_task_manager_plain_queued_pick_computes_next_run_from_history(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("plain-queued-history", {"value": 1})
    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "queued",
            "run_index": 2,
            "start_times": ["2026-01-01_00-00-00", "2026-01-01_00-00-02"],
            "finish_times": ["2026-01-01_00-00-01", "2026-01-01_00-00-03"],
        }),
    )

    manager = _make_task_manager(tasks_dir)

    picked, run_index = manager._pick_queued_task()

    assert picked is not None
    assert picked["name"] == task["name"]
    assert run_index == 3
    assert picked["run_index"] == 3


def test_task_manager_start_batch_tasks_skips_active_tasks(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    tasks = [generator.create_task(f"task-{idx}", {"value": idx}) for idx in range(3)]

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        active = manager._tasks_by_name[tasks[0]["name"]]
        active["status"] = "running"
        active["run_index"] = 1
        manager._mark_running_locked(active["name"], counts_for_batch=True)
        manager._recompute_processing_flag_locked()

    submitted: list[tuple[str, int, bool]] = []

    def fake_submit(target, run_index, *, independent):
        submitted.append((target["name"], run_index, independent))

    monkeypatch.setattr(manager, "_submit_task", fake_submit)

    manager.start_batch_tasks([task["name"] for task in tasks], max_workers=2)

    assert [item[0] for item in submitted] == [tasks[1]["name"]]
    statuses = {task["name"]: task["status"] for task in manager.list_tasks()}
    assert statuses[tasks[0]["name"]] == "running"
    assert statuses[tasks[1]["name"]] == "running"
    assert statuses[tasks[2]["name"]] == "queued"
    assert manager.get_task(tasks[0]["name"])["run_index"] == 1
    queued_info = json.loads((Path(tasks[2]["dir"]) / TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    assert queued_info["run_index"] == 0
    assert "_queued_run_index" not in queued_info


def test_task_manager_run_now_does_not_consume_batch_slots(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    run_now = generator.create_task("run-now", {"value": 1})
    batch = generator.create_task("batch", {"value": 2})

    class CapturingExecutor:
        instances = []

        def __init__(self, max_workers=None):
            self.max_workers = max_workers
            self.submitted = []
            CapturingExecutor.instances.append(self)

        def submit(self, *args, **kwargs):
            self.submitted.append((args, kwargs))
            return Future()

        def shutdown(self, **kwargs):
            pass

    manager = _make_task_manager(tasks_dir)

    monkeypatch.setattr(task_manager_module, "ThreadPoolExecutor", CapturingExecutor)

    manager.start_task_now(run_now["name"])
    assert run_now["name"] in manager._running_ids
    assert run_now["name"] not in manager._batch_running_ids

    manager.start_batch_tasks([batch["name"]], max_workers=1)

    assert manager.get_task(batch["name"])["status"] == "running"
    assert batch["name"] in manager._running_ids
    assert batch["name"] in manager._batch_running_ids
    submitted_names = [
        args[2]
        for executor in CapturingExecutor.instances
        for args, _kwargs in executor.submitted
    ]
    assert submitted_names == ["run-now", "batch"]


def test_task_manager_gpu_auto_queues_and_writes_queue_log_before_assignment(tmp_path, monkeypatch):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join(
            [
                "gpu_scheduler_enabled: true",
                "gpu_scheduler_task_mode: single",
                "gpu_scheduler_gpus_per_task: 1",
                "gpu_scheduler_memory_used_pct: 75",
                "gpu_scheduler_min_free_memory_gb: 8",
                "gpu_scheduler_compute_used_pct: 30",
                "gpu_scheduler_stable_seconds: 6",
                "gpu_scheduler_max_wait_seconds: 86400",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-wait", {"lr": 0.1})

    manager = _make_task_manager(tasks_dir)

    submitted = []
    monkeypatch.setattr(manager, "_submit_task", lambda *args, **kwargs: submitted.append((args, kwargs)))

    manager.start_batch_tasks([task["name"]], max_workers=1)

    assert submitted == []
    queued = manager.get_task(task["name"])
    assert queued["status"] == "queued"
    assert queued["run_index"] == 0
    assert "_queued_run_index" not in queued
    queued_info = json.loads((Path(task["dir"]) / TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    assert queued_info["status"] == "queued"
    assert queued_info["run_index"] == 0
    assert "_queued_run_index" not in queued_info
    queue_log = Path(task["dir"]) / RUN_LOGS_DIR / "queue.log"
    text = queue_log.read_text(encoding="utf-8")
    assert "[PYRUNS] [GPU WAIT] Run #1 waiting for GPU resources" in text
    assert "[PYRUNS]   Run log: run1.log" in text
    assert "max wait=24h" in text


def test_task_manager_gpu_batch_run_waits_each_selected_task(tmp_path, monkeypatch):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join(
            [
                "gpu_scheduler_enabled: true",
                "gpu_scheduler_task_mode: single",
                "gpu_scheduler_memory_used_pct: 40",
                "gpu_scheduler_min_free_memory_gb: 40",
                "gpu_scheduler_compute_used_pct: 30",
                "gpu_scheduler_stable_seconds: 1",
                "gpu_scheduler_max_wait_seconds: 86400",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    generator = TaskGenerator(root_dir=str(tasks_dir))
    tasks = [generator.create_task(f"gpu-batch-{idx}", {"lr": idx}) for idx in range(3)]

    manager = _make_task_manager(tasks_dir)

    submitted = []
    monkeypatch.setattr(manager, "_submit_task", lambda *args, **kwargs: submitted.append((args, kwargs)))

    manager.start_batch_tasks([task["name"] for task in tasks], max_workers=3)

    assert submitted == []
    assert manager.max_workers == 3
    for task in tasks:
        queued = manager.get_task(task["name"])
        assert queued["status"] == "queued"
        assert queued["run_index"] == 0
        queue_log = Path(task["dir"]) / RUN_LOGS_DIR / "queue.log"
        queue_text = queue_log.read_text(encoding="utf-8")
        assert "[PYRUNS] [GPU WAIT] Run #1 waiting for GPU resources" in queue_text
        assert "[PYRUNS]   Run log: run1.log" in queue_text

    now = time.monotonic()
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider([GpuDevice(0, "A800", "GPU-0", 36000, 40960, 1)]),
        clock=lambda: now,
    )
    with manager._lock:
        for task in tasks:
            current = manager._tasks_by_name[task["name"]]
            current["_gpu_last_wait_log_at"] = 0.0

    target, _ = manager._pick_queued_task()

    assert target is None
    for task in tasks:
        queue_bytes = (Path(task["dir"]) / RUN_LOGS_DIR / "queue.log").read_bytes()
        assert b"\r[PYRUNS] Run #1 still waiting after " in queue_bytes
        assert b"blocked: GPU 0 memory" in queue_bytes


def test_task_manager_gpu_auto_assigns_cuda_env_when_queued_task_is_picked(tmp_path):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join(
            [
                "gpu_scheduler_enabled: true",
                "gpu_scheduler_task_mode: multi",
                "gpu_scheduler_gpus_per_task: 2",
                "gpu_scheduler_memory_used_pct: 75",
                "gpu_scheduler_min_free_memory_gb: 8",
                "gpu_scheduler_compute_used_pct: 30",
                "gpu_scheduler_stable_seconds: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-run", {"lr": 0.1})

    manager = _make_task_manager(tasks_dir)

    now = [100.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider(
            [
                GpuDevice(0, "A800", "GPU-0", 2048, 40960, 1),
                GpuDevice(1, "A800", "GPU-1", 4096, 40960, 2),
            ]
        ),
        clock=lambda: now[0],
    )
    manager.start_batch_tasks([task["name"]], max_workers=1)
    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0

    target, run_index = manager._pick_queued_task()

    assert target is not None
    assert run_index == 1
    assert target["_scheduled_env"]["CUDA_VISIBLE_DEVICES"] == "0,1"
    assert target["_scheduled_env"]["PYRUNS_ASSIGNED_GPUS"] == "0,1"
    assert target["_gpu_assignment"]["run_index"] == run_index
    assert target["_gpu_assignment"]["gpu_ids"] == [0, 1]
    assert target["_gpu_assignment"]["env"] == {
        "PYRUNS_ASSIGNED_GPUS": "0,1",
        "CUDA_VISIBLE_DEVICES": "0,1",
    }
    queue_log = Path(task["dir"]) / RUN_LOGS_DIR / "queue.log"
    text = queue_log.read_text(encoding="utf-8")
    assert "[PYRUNS] [GPU ASSIGNED] Run #1 assigned GPUs 0,1" in text
    assert "Run log: run1.log" in text
    assert "CUDA_VISIBLE_DEVICES=0,1" in text
    assert "PYRUNS_ASSIGNED_GPUS=0,1" in text
    assert "Updated at " in text
    assert "Last status at " not in text


def test_task_manager_gpu_scheduler_respects_foreign_running_assignment(tmp_path):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join(
            [
                "gpu_scheduler_enabled: true",
                "gpu_scheduler_task_mode: single",
                "gpu_scheduler_memory_used_pct: 75",
                "gpu_scheduler_min_free_memory_gb: 8",
                "gpu_scheduler_compute_used_pct: 30",
                "gpu_scheduler_stable_seconds: 1",
                "gpu_scheduler_max_tasks_per_gpu: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    generator = TaskGenerator(root_dir=str(tasks_dir))
    remote = generator.create_task("remote-gpu", {"lr": 0.1})
    local = generator.create_task("local-gpu", {"lr": 0.2})
    update_task_info(
        remote["dir"],
        lambda info: info.update({
            "status": "running",
            "run_index": 1,
            "runner_id": "other-host:123:abcdef",
            "runner_host": "other-host",
            "lease_until": time.time() + 60,
            "pids": [12345],
            "_scheduled_env": {"CUDA_VISIBLE_DEVICES": "0", "PYRUNS_ASSIGNED_GPUS": "0"},
            "_gpu_assignment": {
                "task_name": "remote-gpu",
                "run_index": 1,
                "gpu_ids": [0],
                "cuda_visible_devices": "0",
                "env": {"CUDA_VISIBLE_DEVICES": "0", "PYRUNS_ASSIGNED_GPUS": "0"},
                "waited_seconds": 0,
            },
        }),
    )
    update_task_info(local["dir"], lambda info: info.update({"status": "queued"}))

    manager = _make_task_manager(tasks_dir)

    now = [100.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider([GpuDevice(0, "A800", "GPU-0", 2048, 40960, 1)]),
        clock=lambda: now[0],
    )
    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0

    target, _ = manager._pick_queued_task()

    assert target is None
    queued = manager.get_task("local-gpu")
    assert queued["status"] == "queued"
    queue_log = Path(local["dir"]) / RUN_LOGS_DIR / "queue.log"
    assert "GPU 0 reserved (1/1)" in queue_log.read_text(encoding="utf-8")


def test_task_manager_gpu_scheduler_respects_undiscovered_foreign_assignment(tmp_path):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join(
            [
                "gpu_scheduler_enabled: true",
                "gpu_scheduler_task_mode: single",
                "gpu_scheduler_memory_used_pct: 75",
                "gpu_scheduler_min_free_memory_gb: 8",
                "gpu_scheduler_compute_used_pct: 30",
                "gpu_scheduler_stable_seconds: 1",
                "gpu_scheduler_max_tasks_per_gpu: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    generator = TaskGenerator(root_dir=str(tasks_dir))
    local = generator.create_task("local-gpu", {"lr": 0.2})

    manager = _make_task_manager(tasks_dir)

    remote = generator.create_task("remote-gpu", {"lr": 0.1})
    update_task_info(
        remote["dir"],
        lambda info: info.update({
            "status": "running",
            "run_index": 1,
            "runner_id": "other-host:123:abcdef",
            "runner_host": "other-host",
            "lease_until": time.time() + 60,
            "pids": [12345],
            "_scheduled_env": {"CUDA_VISIBLE_DEVICES": "0", "PYRUNS_ASSIGNED_GPUS": "0"},
            "_gpu_assignment": {
                "task_name": "remote-gpu",
                "run_index": 1,
                "gpu_ids": [0],
                "cuda_visible_devices": "0",
                "env": {"CUDA_VISIBLE_DEVICES": "0", "PYRUNS_ASSIGNED_GPUS": "0"},
                "waited_seconds": 0,
            },
        }),
    )
    update_task_info(local["dir"], lambda info: info.update({"status": "queued"}))
    manager.refresh_from_disk(task_ids=["local-gpu"], force_all=True)

    now = [100.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider([GpuDevice(0, "A800", "GPU-0", 2048, 40960, 1)]),
        clock=lambda: now[0],
    )
    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0

    target, _ = manager._pick_queued_task()

    assert [task["name"] for task in manager.tasks] == ["local-gpu"]
    assert target is None
    queued = manager.get_task("local-gpu")
    assert queued["status"] == "queued"
    queue_log = Path(local["dir"]) / RUN_LOGS_DIR / "queue.log"
    assert "GPU 0 reserved (1/1)" in queue_log.read_text(encoding="utf-8")


def test_task_manager_gpu_claim_failure_restores_disk_state(tmp_path, monkeypatch):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join(
            [
                "gpu_scheduler_enabled: true",
                "gpu_scheduler_task_mode: single",
                "gpu_scheduler_memory_used_pct: 75",
                "gpu_scheduler_min_free_memory_gb: 8",
                "gpu_scheduler_compute_used_pct: 30",
                "gpu_scheduler_stable_seconds: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("claim-race", {"lr": 0.1})
    update_task_info(task["dir"], lambda info: info.update({"status": "queued"}))

    manager = _make_task_manager(tasks_dir)

    now = [100.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider([GpuDevice(0, "A800", "GPU-0", 2048, 40960, 1)]),
        clock=lambda: now[0],
    )
    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0
    monkeypatch.setattr(manager, "_claim_task_for_run", lambda *args, **kwargs: None)

    target, _ = manager._pick_queued_task()

    assert target is None
    refreshed = manager.get_task("claim-race")
    assert refreshed["status"] == "queued"
    assert "_scheduled_env" not in refreshed
    assert "_gpu_assignment" not in refreshed
    assert "claim-race" not in manager._running_ids


def test_task_manager_gpu_wait_does_not_advance_public_run_index_until_assignment(tmp_path):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join(
            [
                "gpu_scheduler_enabled: true",
                "gpu_scheduler_task_mode: single",
                "gpu_scheduler_memory_used_pct: 75",
                "gpu_scheduler_min_free_memory_gb: 8",
                "gpu_scheduler_compute_used_pct: 30",
                "gpu_scheduler_stable_seconds: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-public-index", {"lr": 0.1})
    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "completed",
            "run_index": 1,
            "start_times": ["2026-01-01_00-00-00"],
            "finish_times": ["2026-01-01_00-00-01"],
        }),
    )

    manager = _make_task_manager(tasks_dir)

    manager.start_batch_tasks([task["name"]], max_workers=1)

    queued = manager.get_task(task["name"])
    assert queued["status"] == "queued"
    assert queued["run_index"] == 1
    assert "_queued_run_index" not in queued
    queued_info = load_task_info(task["dir"])
    assert queued_info["status"] == "queued"
    assert queued_info["run_index"] == 1
    assert "_queued_run_index" not in queued_info

    now = [100.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider([GpuDevice(0, "A800", "GPU-0", 1024, 40960, 0)]),
        clock=lambda: now[0],
    )
    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0
    target, run_index = manager._pick_queued_task()

    assert target is not None
    assert run_index == 2
    assert target["run_index"] == 2
    assert "_queued_run_index" not in target


def test_task_manager_gpu_auto_independent_task_can_bypass_full_batch_slots(tmp_path):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join(
            [
                "gpu_scheduler_enabled: true",
                "gpu_scheduler_task_mode: single",
                "gpu_scheduler_memory_used_pct: 75",
                "gpu_scheduler_min_free_memory_gb: 8",
                "gpu_scheduler_compute_used_pct: 30",
                "gpu_scheduler_stable_seconds: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    generator = TaskGenerator(root_dir=str(tasks_dir))
    batch_task = generator.create_task("batch-wait", {"lr": 0.1})
    run_now_task = generator.create_task("run-now", {"lr": 0.2})

    manager = _make_task_manager(tasks_dir)

    manager.max_workers = 1
    manager._mark_running_locked("already-running", counts_for_batch=True)
    now = [300.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider([
            GpuDevice(0, "A800", "GPU-0", 1024, 40960, 0),
        ]),
        clock=lambda: now[0],
    )
    with manager._lock:
        manager._tasks_by_name[batch_task["name"]]["status"] = "queued"
        manager._tasks_by_name[batch_task["name"]]["run_index"] = 1
        manager._tasks_by_name[batch_task["name"]]["_gpu_wait_started_at"] = 290.0
        manager._tasks_by_name[run_now_task["name"]]["status"] = "queued"
        manager._tasks_by_name[run_now_task["name"]]["run_index"] = 1
        manager._tasks_by_name[run_now_task["name"]]["_gpu_wait_started_at"] = 290.0
        manager._tasks_by_name[run_now_task["name"]]["_queued_independent"] = True
        manager._recompute_processing_flag_locked()
    update_task_info(batch_task["dir"], lambda info: info.update({"status": "queued"}))
    update_task_info(run_now_task["dir"], lambda info: info.update({"status": "queued"}))

    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0
    target, run_index = manager._pick_queued_task(independent_only=True)

    assert target is not None
    assert target["name"] == "run-now"
    assert run_index == 1
    assert "run-now" in manager._running_ids
    assert "run-now" not in manager._batch_running_ids
    assert manager.get_task(batch_task["name"])["status"] == "queued"


def test_task_manager_gpu_independent_submit_does_not_consume_batch_slots(tmp_path, monkeypatch):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join(
            [
                "gpu_scheduler_enabled: true",
                "gpu_scheduler_task_mode: single",
                "gpu_scheduler_memory_used_pct: 75",
                "gpu_scheduler_min_free_memory_gb: 8",
                "gpu_scheduler_compute_used_pct: 30",
                "gpu_scheduler_stable_seconds: 1",
                "gpu_scheduler_max_tasks_per_gpu: 2",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    generator = TaskGenerator(root_dir=str(tasks_dir))
    batch_task = generator.create_task("batch", {"lr": 0.1})
    run_now_task = generator.create_task("run-now", {"lr": 0.2})

    class CapturingExecutor:
        def __init__(self, max_workers=None):
            self.max_workers = max_workers
            self.submitted = []

        def submit(self, *args, **kwargs):
            self.submitted.append((args, kwargs))
            return Future()

        def shutdown(self, **kwargs):
            pass

    manager = _make_task_manager(tasks_dir)

    monkeypatch.setattr(task_manager_module, "ThreadPoolExecutor", CapturingExecutor)
    manager.max_workers = 1
    now = [300.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider([
            GpuDevice(0, "A800", "GPU-0", 1024, 40960, 0),
        ]),
        clock=lambda: now[0],
    )
    with manager._lock:
        manager._tasks_by_name[batch_task["name"]]["status"] = "queued"
        manager._tasks_by_name[batch_task["name"]]["run_index"] = 1
        manager._tasks_by_name[batch_task["name"]]["_gpu_wait_started_at"] = 290.0
        manager._tasks_by_name[run_now_task["name"]]["status"] = "queued"
        manager._tasks_by_name[run_now_task["name"]]["run_index"] = 1
        manager._tasks_by_name[run_now_task["name"]]["_gpu_wait_started_at"] = 290.0
        manager._tasks_by_name[run_now_task["name"]]["_queued_independent"] = True
        manager._recompute_processing_flag_locked()
    update_task_info(batch_task["dir"], lambda info: info.update({"status": "queued"}))
    update_task_info(run_now_task["dir"], lambda info: info.update({"status": "queued"}))

    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0
    independent_target, run_index = manager._pick_queued_task(independent_only=True)
    assert independent_target is not None
    independent = bool(independent_target.pop("_queued_independent", False))
    manager._submit_task(independent_target, run_index, independent=independent)

    assert run_now_task["name"] in manager._running_ids
    assert run_now_task["name"] not in manager._batch_running_ids

    batch_target, batch_run_index = manager._pick_queued_task()

    assert batch_target is not None
    assert batch_target["name"] == batch_task["name"]
    assert batch_run_index == 1
    assert batch_task["name"] in manager._running_ids
    assert batch_task["name"] in manager._batch_running_ids


def test_task_manager_start_task_now_queues_gpu_task_as_independent(tmp_path, monkeypatch):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join([
            "gpu_scheduler_enabled: true",
            "gpu_scheduler_memory_used_pct: 99",
            "gpu_scheduler_min_free_memory_gb: 0.5",
            "gpu_scheduler_compute_used_pct: 30",
            "gpu_scheduler_stable_seconds: 1",
        ])
        + "\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("run-now-gpu", {"lr": 0.1})

    manager = _make_task_manager(tasks_dir)

    submitted = []
    monkeypatch.setattr(manager, "_submit_task", lambda *args, **kwargs: submitted.append((args, kwargs)))
    manager.start_task_now(task["name"])

    queued = manager.get_task(task["name"])
    assert submitted == []
    assert queued["status"] == "queued"
    assert queued["_queued_independent"] is True
    assert (Path(task["dir"]) / RUN_LOGS_DIR / "queue.log").exists()


def test_task_manager_clears_stale_gpu_schedule_env_before_plain_rerun(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-stale-env", {"lr": 0.1})

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        target = manager._tasks_by_name[task["name"]]
        target["_scheduled_env"] = {"CUDA_VISIBLE_DEVICES": "7", "PYRUNS_ASSIGNED_GPUS": "7"}
        target["_gpu_assignment"] = {"gpu_ids": [7]}
        target["_gpu_wait_started_at"] = 1.0
        target["_gpu_last_wait_log_at"] = 1.0
        target["_queued_independent"] = True

    submitted = []

    def fake_submit(target, run_index, *, independent):
        submitted.append(dict(target))

    monkeypatch.setattr(manager, "_submit_task", fake_submit)
    manager.start_batch_tasks([task["name"]], max_workers=1)

    assert len(submitted) == 1
    assert "_scheduled_env" not in submitted[0]
    assert "_gpu_assignment" not in submitted[0]
    assert "_gpu_wait_started_at" not in submitted[0]
    assert "_gpu_last_wait_log_at" not in submitted[0]
    assert "_queued_independent" not in submitted[0]


@pytest.mark.parametrize("final_status", ["completed", "failed", "cancelled"])
def test_task_manager_plain_rerun_does_not_create_gpu_wait_state(tmp_path, final_status):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("plain-rerun", {"lr": 0.1})
    update_task_info(task["dir"], lambda info: info.update({"status": final_status, "run_index": 1}))

    manager = _make_task_manager(tasks_dir)

    assert manager.rerun_task(task["name"]) is True

    queued = manager.get_task(task["name"])
    assert queued["status"] == "queued"
    assert "_gpu_wait_started_at" not in queued
    assert "_queued_independent" not in queued
    assert not (Path(task["dir"]) / RUN_LOGS_DIR / "queue.log").exists()


def test_task_manager_on_task_done_clears_gpu_schedule_state(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-done", {"lr": 0.1})

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        target = manager._tasks_by_name[task["name"]]
        target["status"] = "running"
        target["run_index"] = 1
        target["_scheduled_env"] = {"CUDA_VISIBLE_DEVICES": "0"}
        target["_gpu_assignment"] = {"gpu_ids": [0]}
        target["_queued_independent"] = True
        manager._mark_running_locked(task["name"], counts_for_batch=False)
    update_task_info(
        task["dir"],
        lambda info: info.update({"status": "completed", "run_index": 1}),
    )

    future = Future()
    future.set_result({"status": "completed"})
    manager._on_task_done(
        future,
        task["name"],
        expected_runner_id=manager.runner_id,
        expected_run_index=1,
    )

    refreshed = manager.get_task(task["name"])
    assert "_scheduled_env" not in refreshed
    assert "_gpu_assignment" not in refreshed
    assert "_queued_independent" not in refreshed
    assert task["name"] not in manager._running_ids
    assert task["name"] not in manager._batch_running_ids


def test_task_manager_gpu_auto_respects_existing_cuda_visible_devices_in_task_env(tmp_path):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join(
            [
                "gpu_scheduler_enabled: true",
                "gpu_scheduler_task_mode: multi",
                "gpu_scheduler_gpus_per_task: 2",
                "gpu_scheduler_memory_used_pct: 40",
                "gpu_scheduler_min_free_memory_gb: 40",
                "gpu_scheduler_compute_used_pct: 30",
                "gpu_scheduler_stable_seconds: 1",
                "gpu_scheduler_respect_cuda_visible_devices: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-fixed", {"lr": 0.1})
    update_task_info(str(Path(task["dir"])), lambda info: info.update({"env": {"CUDA_VISIBLE_DEVICES": "1,2"}}))

    manager = _make_task_manager(tasks_dir)

    now = [200.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider(
            [
                GpuDevice(0, "A800", "GPU-0", 1024, 81920, 1),
                GpuDevice(1, "A800", "GPU-1", 1024, 81920, 1),
                GpuDevice(2, "A800", "GPU-2", 1024, 81920, 1),
            ]
        ),
        clock=lambda: now[0],
    )
    manager.start_batch_tasks([task["name"]], max_workers=1)
    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0

    target, _ = manager._pick_queued_task()

    assert target is not None
    assert target["_gpu_assignment"]["gpu_ids"] == [1, 2]
    assert target["_scheduled_env"] == {"PYRUNS_ASSIGNED_GPUS": "1,2"}


def test_task_manager_gpu_auto_times_out_waiting_tasks_and_writes_logs(tmp_path):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join(
            [
                "gpu_scheduler_enabled: true",
                "gpu_scheduler_task_mode: single",
                "gpu_scheduler_memory_used_pct: 40",
                "gpu_scheduler_min_free_memory_gb: 40",
                "gpu_scheduler_compute_used_pct: 30",
                "gpu_scheduler_stable_seconds: 15",
                "gpu_scheduler_max_wait_seconds: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-timeout", {"lr": 0.1})

    manager = _make_task_manager(tasks_dir)

    manager.start_batch_tasks([task["name"]], max_workers=1)
    with manager._lock:
        manager._tasks_by_name[task["name"]]["_gpu_wait_started_at"] = time.monotonic() - 10

    target, _ = manager._pick_queued_task()

    assert target is None
    assert manager.get_task(task["name"])["status"] == "failed"
    info = load_task_info(task["dir"])
    assert info["run_index"] == 0
    assert info["start_times"] == []
    assert info["finish_times"] == []
    log_dir = Path(task["dir"]) / RUN_LOGS_DIR
    queue_text = (log_dir / "queue.log").read_text(encoding="utf-8")
    error_text = (log_dir / ERROR_LOG_FILENAME).read_text(encoding="utf-8")
    assert "[PYRUNS] [GPU WAIT TIMEOUT] Run #1 GPU wait timed out" in queue_text
    assert "max wait=1s" in queue_text
    assert "Queued task failed" in error_text
    assert "Run #1 failed" not in error_text
    assert "reason=gpu_wait_timeout" in error_text


def test_task_manager_gpu_wait_timeout_preserves_task_when_foreign_runner_renews(tmp_path, monkeypatch):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join(
            [
                "gpu_scheduler_enabled: true",
                "gpu_scheduler_stable_seconds: 15",
                "gpu_scheduler_max_wait_seconds: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-timeout-race", {"lr": 0.1})

    manager = _make_task_manager(tasks_dir)

    manager.start_batch_tasks([task["name"]], max_workers=1)
    foreign_runner = "other-host:4321:abcdef"
    update_task_info(
        task["dir"],
        lambda info: info.update(
            {
                "runner_id": foreign_runner,
                "runner_host": "other-host",
                "lease_until": time.time() - 60,
            }
        ),
    )
    manager.refresh_from_disk(check_all=True)
    with manager._lock:
        manager._tasks_by_name[task["name"]]["_gpu_wait_started_at"] = time.monotonic() - 10

    original_mark_failed = manager._mark_failed_on_disk

    def renew_before_timeout(task_ref, **kwargs):
        update_task_info(
            task["dir"],
            lambda info: info.update({"lease_until": time.time() + 60}),
        )
        return original_mark_failed(task_ref, **kwargs)

    monkeypatch.setattr(manager, "_mark_failed_on_disk", renew_before_timeout)

    target, run_index = manager._pick_queued_task()

    assert target is None
    assert run_index == 1
    refreshed = manager.get_task(task["name"])
    assert refreshed["status"] == "queued"
    assert refreshed["runner_id"] == foreign_runner
    info = load_task_info(task["dir"])
    assert info["status"] == "queued"
    assert info["runner_id"] == foreign_runner
    queue_text = (Path(task["dir"]) / RUN_LOGS_DIR / "queue.log").read_text(encoding="utf-8")
    assert "GPU WAIT TIMEOUT" not in queue_text
    assert not (Path(task["dir"]) / RUN_LOGS_DIR / ERROR_LOG_FILENAME).exists()


def test_task_manager_queued_placeholder_run_slot_is_trimmed_before_next_assignment(tmp_path):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join([
            "gpu_scheduler_enabled: true",
            "gpu_scheduler_memory_used_pct: 99",
            "gpu_scheduler_min_free_memory_gb: 0.5",
            "gpu_scheduler_compute_used_pct: 30",
            "gpu_scheduler_stable_seconds: 1",
        ])
        + "\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-placeholder", {"lr": 0.1})
    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "queued",
            "run_index": 2,
            "start_times": ["2026-01-01_00-00-00", ""],
            "finish_times": ["2026-01-01_00-00-01", ""],
            "pids": [123, None],
            "records": [{"loss": 1.0}, {}],
            "tracks": [{}, {}],
            "_queued_run_index": 2,
        }),
    )

    manager = _make_task_manager(tasks_dir)

    queued = manager.get_task(task["name"])
    assert queued["status"] == "queued"
    assert queued["run_index"] == 1
    assert queued["start_times"] == ["2026-01-01_00-00-00"]
    assert "_queued_run_index" not in queued

    now = [100.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider([GpuDevice(0, "A800", "GPU-0", 1024, 40960, 0)]),
        clock=lambda: now[0],
    )
    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0

    target, run_index = manager._pick_queued_task()

    assert target is not None
    assert run_index == 2
    assert target["run_index"] == 2
    assert target["_gpu_assignment"]["run_index"] == 2


def test_task_manager_cancel_queued_task_does_not_create_run_slot(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("queued-cancel", {"lr": 0.1})
    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "queued",
            "run_index": 1,
            "start_times": ["2026-01-01_00-00-00", ""],
            "finish_times": ["2026-01-01_00-00-01", ""],
            "pids": [123, None],
            "records": [{}, {}],
            "tracks": [{}, {}],
        }),
    )

    manager = _make_task_manager(tasks_dir)

    assert manager.cancel_task(task["name"]) is True

    info = load_task_info(task["dir"])
    assert info["status"] == "cancelled"
    assert info["run_index"] == 1
    assert info["start_times"] == ["2026-01-01_00-00-00"]
    assert info["finish_times"] == ["2026-01-01_00-00-01"]
    error_text = (Path(task["dir"]) / RUN_LOGS_DIR / ERROR_LOG_FILENAME).read_text(encoding="utf-8")
    assert "Queued task stopped" in error_text
    assert "Run #2 stopped" not in error_text
    assert "reason=cancelled_by_user" in error_text
    assert "previous_status=queued" in error_text


def test_task_manager_cancel_task_persists_reason_before_verified_termination(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "runner"
    task_dir.mkdir()
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda pid: True)
    _write_running_config_task(task_dir)

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "runner", task_dir)

    events = []
    original_persist = manager._persist_pending_stop_summary

    def record_persist(*args, **kwargs):
        events.append("persist")
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(manager, "_persist_pending_stop_summary", record_persist)
    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda pid, expected_create_time=None: events.append(
            ("kill", pid, expected_create_time)
        ) or True,
    )

    assert manager.cancel_task("runner") is True

    info = json.loads((task_dir / TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    assert info["status"] == "running"
    assert info["_pending_stop_summary"]["reason"] == "cancelled_by_user"
    assert info["_pending_stop_summary"]["detail_lines"] == ["previous_status=running"]
    assert events == ["persist", ("kill", 12345, 1000.0)]


def test_task_manager_request_cancel_keeps_retrying_a_persisted_local_request(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "runner"
    task_dir.mkdir()
    _write_running_config_task(task_dir)

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "runner", task_dir)
    monkeypatch.setattr(manager, "cancel_task", lambda *_args, **_kwargs: False)

    assert manager.request_task_cancel(
        "runner",
        expected_runner_id=manager.runner_id,
        expected_run_index=1,
    ) is False
    info = load_task_info(str(task_dir))
    assert info["status"] == "running"
    assert info["cancel_requested_at"]


def test_task_manager_request_cancel_rejects_unverified_stop_with_stale_marker(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "runner"
    task_dir.mkdir()
    _write_running_config_task(task_dir)

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "runner", task_dir)
    monkeypatch.setattr("pyruns.core.task_manager.kill_process", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(manager, "_clear_pending_stop_request", lambda *_args, **_kwargs: False)

    assert manager.request_task_cancel(
        "runner",
        expected_runner_id=manager.runner_id,
        expected_run_index=1,
    ) is False
    info = load_task_info(str(task_dir))
    assert info["cancel_requested_at"]
    assert info["_pending_stop_summary"]["reason"] == "cancelled_by_user"


def test_task_manager_cancel_task_fails_closed_when_task_info_is_busy(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "runner"
    task_dir.mkdir()
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda pid: True)
    _write_running_config_task(task_dir)

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "runner", task_dir)

    killed = []
    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda pid, expected_create_time=None: killed.append((pid, expected_create_time)) or True,
    )

    with patch("pyruns.core.task_manager.update_task_metadata", side_effect=TimeoutError("busy")):
        assert manager.cancel_task("runner") is False

    assert manager.get_task("runner")["status"] == "running"
    assert killed == []


def test_task_manager_cancel_task_uses_short_task_info_lock(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "runner"
    task_dir.mkdir()
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda pid: True)
    _write_running_config_task(task_dir)

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "runner", task_dir)

    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda pid, expected_create_time=None: True,
    )
    timeout_values = []

    def record_timeout(task_dir, updater, **kwargs):
        timeout_values.append(kwargs.get("timeout_sec"))
        raise TimeoutError("busy")

    with patch("pyruns.core.task_manager.update_task_metadata", side_effect=record_timeout):
        assert manager.cancel_task("runner") is False

    assert timeout_values == [task_manager_module._STOP_TASK_INFO_LOCK_TIMEOUT_SEC]


def test_task_manager_cancel_task_does_not_finalize_or_kill_a_reused_pid(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "reused"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "reused",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [12345],
            "pid_create_times": [1000.0],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "reused", task_dir)

    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda _pid, expected_create_time=None: False,
    )

    assert manager.cancel_task("reused") is False
    info = load_task_info(str(task_dir))
    assert info["status"] == "running"
    assert "_pending_stop_summary" not in info


def test_task_manager_cancel_task_keeps_request_for_a_live_process_after_timeout(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "retry-stop"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "retry-stop",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [12345],
            "pid_create_times": [1000.0],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "retry-stop", task_dir)
    monkeypatch.setattr("pyruns.core.task_manager.kill_process", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        "pyruns.core.task_manager.process_identity_matches",
        lambda *_args, **_kwargs: True,
    )

    assert manager.cancel_task("retry-stop") is False
    info = load_task_info(str(task_dir))
    assert info["status"] == "running"
    assert info["cancel_requested_at"]
    assert info["_pending_stop_summary"]["reason"] == "cancelled_by_user"


def test_kill_process_rejects_mismatched_creation_time_without_signalling(monkeypatch):
    from pyruns.utils import process_utils

    monkeypatch.setattr(
        process_utils,
        "process_identity_matches",
        lambda pid, expected_create_time: False,
    )
    monkeypatch.setattr(
        process_utils.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("a reused PID must not be signalled"),
    )

    assert process_utils.kill_process(12345, expected_create_time=1000.0) is False


def test_task_manager_cancel_task_refreshes_completed_disk_state(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "race"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "race",
            "status": "queued",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 0,
            "start_times": [],
            "finish_times": [],
            "pids": [],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    manager = _make_task_manager(tasks_dir)

    update_task_info(
        str(task_dir),
        lambda info: info.update(
            {
                "status": "completed",
                "progress": 1.0,
                "start_times": ["2026-03-20_00-00-01"],
                "finish_times": ["2026-03-20_00-00-02"],
            }
        ),
    )

    assert manager.cancel_task("race") is False
    assert manager.get_task("race")["status"] == "completed"
    assert load_task_info(str(task_dir))["status"] == "completed"


def test_task_manager_cancel_foreign_live_runner_preserves_owner_and_gpu(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "foreign"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "foreign",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [12345],
            "records": [],
            "tracks": [],
            "runner_id": "other-host:123:abcdef",
            "runner_host": "other-host",
            "lease_heartbeat": time.time(),
            "lease_until": time.time() + 60,
            "_gpu_assignment": {"device_ids": ["0"]},
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    manager = _make_task_manager(tasks_dir)

    assert manager.cancel_task("foreign") is False
    info = load_task_info(str(task_dir))
    assert info["status"] == "running"
    assert info["runner_id"] == "other-host:123:abcdef"
    assert info["_gpu_assignment"] == {"device_ids": ["0"]}


@pytest.mark.parametrize(
    ("task_pid", "expected_killed"),
    [(12345, [12345]), (os.getpid(), [])],
)
def test_task_manager_shutdown_does_not_recreate_deleted_owned_task(
    tmp_path, monkeypatch, task_pid, expected_killed
):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "owned"
    task_dir.mkdir()
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda _pid: True)
    monkeypatch.setattr(
        "pyruns.core.task_manager.process_identity_matches",
        lambda _pid, _created_at: True,
    )

    manager = _make_task_manager(tasks_dir, lazy_scan=None)

    save_task_info(
        str(task_dir),
        {
            "name": "owned",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [task_pid],
            "pid_create_times": [1000.0],
            "records": [],
            "tracks": [],
            "runner_id": manager.runner_id,
            "runner_host": manager.runner_host,
            "lease_heartbeat": time.time(),
            "lease_until": time.time() + 60,
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})
    manager.scan_disk()
    shutil.rmtree(task_dir)
    killed = []
    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda pid, expected_create_time=None: killed.append(pid) or True,
    )

    manager.shutdown()

    assert killed == expected_killed
    assert not task_dir.exists()


def test_task_manager_delete_running_task_kills_outside_lock(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "runner"
    task_dir.mkdir()
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda pid: True)
    _write_running_config_task(task_dir)

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "runner", task_dir)

    lock_checks = []

    def fake_kill(pid, expected_create_time=None):
        acquired = manager._lock.acquire(blocking=False)
        lock_checks.append(acquired)
        if acquired:
            manager._lock.release()
        assert expected_create_time == 1000.0
        update_task_info(
            str(task_dir),
            lambda info: info.update({"status": "cancelled"}),
        )
        with manager._lock:
            manager._clear_running_locked("runner")
        return True

    monkeypatch.setattr("pyruns.core.task_manager.kill_process", fake_kill)

    assert manager.delete_tasks(["runner"]) == ["runner"]
    assert lock_checks == [True]


def test_task_manager_delete_running_task_stays_put_when_process_survives(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "runner"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "runner",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [12345],
            "pid_create_times": [1000.0],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "runner", task_dir)
    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda _pid, expected_create_time=None: False,
    )

    assert manager.delete_tasks(["runner"]) == []
    assert task_dir.exists()
    assert not (tasks_dir / TRASH_DIR / "runner").exists()
    assert load_task_info(str(task_dir))["status"] == "running"


def test_task_manager_delete_active_task_fails_closed_when_task_info_is_busy(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "queued"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "queued",
            "status": "queued",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 0,
            "start_times": [],
            "finish_times": [],
            "pids": [],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    manager = _make_task_manager(tasks_dir)

    monkeypatch.setattr(manager, "_mark_failed_on_disk", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("busy")))

    assert manager.delete_tasks(["queued"]) == []
    assert manager.get_task("queued") is not None
    assert task_dir.exists()
    assert not (tasks_dir / TRASH_DIR / "queued").exists()


def test_task_manager_delete_completed_disk_state_does_not_mark_failed(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "done"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "done",
            "status": "queued",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 0,
            "start_times": [],
            "finish_times": [],
            "pids": [],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    manager = _make_task_manager(tasks_dir)

    update_task_info(
        str(task_dir),
        lambda info: info.update(
            {
                "status": "completed",
                "progress": 1.0,
                "start_times": ["2026-03-20_00-00-01"],
                "finish_times": ["2026-03-20_00-00-02"],
            }
        ),
    )

    assert manager.delete_tasks(["done"]) == ["done"]
    moved_info = load_task_info(str(tasks_dir / TRASH_DIR / "done"))
    assert moved_info["status"] == "completed"
    assert manager.get_task("done") is None


def test_task_manager_delete_foreign_live_runner_preserves_task(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "foreign"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "foreign",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [12345],
            "records": [],
            "tracks": [],
            "runner_id": "other-host:123:abcdef",
            "runner_host": "other-host",
            "lease_heartbeat": time.time(),
            "lease_until": time.time() + 60,
            "_gpu_assignment": {"device_ids": ["0"]},
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    manager = _make_task_manager(tasks_dir)

    assert manager.delete_tasks(["foreign"]) == []
    assert task_dir.exists()
    assert not (tasks_dir / TRASH_DIR / "foreign").exists()
    info = load_task_info(str(task_dir))
    assert info["status"] == "running"
    assert info["runner_id"] == "other-host:123:abcdef"
    assert info["_gpu_assignment"] == {"device_ids": ["0"]}
    assert manager.get_task("foreign")["status"] == "running"


def test_task_manager_shutdown_cleanup_kills_only_running_task_latest_pid(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    running_dir = tasks_dir / "runner"
    queued_dir = tasks_dir / "queued"
    prestart_dir = tasks_dir / "prestart"
    missing_dir = tasks_dir / "missing"
    running_dir.mkdir()
    queued_dir.mkdir()
    prestart_dir.mkdir()
    missing_dir.mkdir()

    save_task_info(
        str(running_dir),
        {
            "name": "runner",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [111, 222],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(running_dir / CONFIG_FILENAME), {"lr": 0.01})
    save_task_info(
        str(queued_dir),
        {
            "name": "queued",
            "status": "queued",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 0,
            "start_times": [],
            "finish_times": [],
            "pids": [333],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(queued_dir / CONFIG_FILENAME), {"lr": 0.02})
    save_task_info(
        str(prestart_dir),
        {
            "name": "prestart",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": [""],
            "finish_times": [""],
            "pids": [None],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(prestart_dir / CONFIG_FILENAME), {"lr": 0.03})
    save_task_info(
        str(missing_dir),
        {
            "name": "missing",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": [""],
            "finish_times": [""],
            "pids": [444],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(missing_dir / CONFIG_FILENAME), {"lr": 0.04})

    killed: list[int] = []
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda pid: True)
    def kill_for_shutdown(pid, expected_create_time=None):
        killed.append(pid)
        return pid != 444

    monkeypatch.setattr("pyruns.core.task_manager.kill_process", kill_for_shutdown)
    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "runner", running_dir, pids=[111, 222])
    _mark_task_owned_by_manager(manager, "missing", missing_dir, pids=[444])
    update_task_info(
        str(queued_dir),
        lambda info: info.update(
            {
                "runner_id": manager.runner_id,
                "runner_host": manager.runner_host,
                "lease_heartbeat": time.time(),
                "lease_until": time.time() + 60,
            }
        ),
    )
    update_task_info(
        str(prestart_dir),
        lambda info: info.update(
            {
                "runner_id": manager.runner_id,
                "runner_host": manager.runner_host,
                "lease_heartbeat": time.time(),
                "lease_until": time.time() + 60,
            }
        ),
    )
    manager.refresh_from_disk(task_ids=["prestart"], force_all=True)
    with manager._lock:
        manager._mark_running_locked("prestart", counts_for_batch=True)
    shutil.rmtree(missing_dir)

    manager._cleanup_on_shutdown()

    assert sorted(killed) == [222, 444]
    running_info = json.loads((running_dir / TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    queued_info = json.loads((queued_dir / TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    prestart_info = load_task_info(str(prestart_dir))
    assert running_info["status"] == "failed"
    assert queued_info["status"] == "failed"
    assert prestart_info["status"] == "running"
    assert prestart_info["_pending_stop_summary"]["reason"] == "system_shutdown"
    assert manager.get_task("missing")["status"] == "running"


def test_task_manager_shutdown_cleanup_ignores_malformed_in_memory_tasks(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    monkeypatch.setattr("pyruns.core.task_manager.kill_process", lambda pid: None)
    manager = _make_task_manager(tasks_dir)

    manager.tasks = [{}, {"name": "missing-status"}, None]

    manager._cleanup_on_shutdown()

    assert manager.tasks == [{}, {"name": "missing-status"}, None]


def test_task_manager_shutdown_does_not_overwrite_newer_final_disk_status(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("finished-elsewhere", {"value": 1})

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        manager._tasks_by_name[task["name"]]["status"] = "running"
    update_task_info(task["dir"], lambda info: info.update({"status": "completed"}))

    manager._cleanup_on_shutdown()

    assert load_task_info(task["dir"])["status"] == "completed"


def test_task_manager_shutdown_does_not_overwrite_new_run_claimed_during_cleanup(
    tmp_path,
    monkeypatch,
):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("reclaimed", {"value": 1})

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, task["name"], Path(task["dir"]))
    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda _pid, expected_create_time=None: True,
    )

    original_mark_failed = manager._mark_failed_on_disk

    def claim_new_run_after_terminal_write(task_ref, **kwargs):
        original_mark_failed(task_ref, **kwargs)

        def _claim(info):
            info["status"] = "running"
            info["run_index"] = 2
            info["runner_id"] = "other-host:5252:new-run"
            info["runner_host"] = "other-host"
            info["lease_heartbeat"] = time.time()
            info["lease_until"] = time.time() + 60

        updated = update_task_info(task["dir"], _claim)
        with manager._lock:
            current = manager._tasks_by_name[task["name"]]
            manager._apply_info_to_task(current, updated)
            manager._mark_running_locked(task["name"], counts_for_batch=True)

    monkeypatch.setattr(manager, "_mark_failed_on_disk", claim_new_run_after_terminal_write)

    manager._cleanup_on_shutdown()

    info = load_task_info(task["dir"])
    assert info["status"] == "running"
    assert info["run_index"] == 2
    assert info["runner_id"] == "other-host:5252:new-run"
    assert manager.get_task(task["name"])["status"] == "running"


def test_foreign_queued_runner_lease_survives_observer_shutdown(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    first = generator.create_task("first", {"value": 1})
    second = generator.create_task("second", {"value": 2})

    with (
        patch.object(TaskManager, "_scheduler_loop", lambda self: None),
        patch.object(TaskManager, "_submit_task", lambda self, *args, **kwargs: None),
    ):
        owner = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)
        owner.start_batch_tasks([first["name"], second["name"]], max_workers=1)
        observer = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    queued_info = load_task_info(second["dir"])
    assert queued_info["status"] == "queued"
    assert queued_info["runner_id"] == owner.runner_id

    observer._cleanup_on_shutdown()

    after = load_task_info(second["dir"])
    assert after["status"] == "queued"
    assert after["runner_id"] == owner.runner_id
    owner.shutdown()
    observer.shutdown()


def test_task_manager_shutdown_unregisters_atexit_callback(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    manager = _make_task_manager(tasks_dir)

    manager_ref = weakref.ref(manager)
    manager.shutdown()
    manager._scheduler_thread.join(timeout=1)
    del manager
    gc.collect()

    assert manager_ref() is None


def test_task_manager_observers_serialization_and_missing_root_scan(tmp_path):
    missing_tasks_dir = tmp_path / "missing"
    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(missing_tasks_dir), lazy_scan=False)

    assert manager.list_tasks() == []
    assert TaskManager.serialize_task(None) is None

    calls = []

    def good_callback():
        calls.append("good")

    def bad_callback():
        calls.append("bad")
        raise RuntimeError("observer failed")

    manager.on_change(good_callback)
    manager.on_change(good_callback)
    manager.on_change(bad_callback)
    manager.trigger_update()
    manager.off_change(bad_callback)
    manager.trigger_update()

    assert calls == ["good", "bad", "good"]
    summary = TaskManager.serialize_task(
        {
            "dir": r"C:\tmp\task",
            "name": "alpha",
            "status": "running",
            "config": {"lr": 0.1},
            "env": {"A": "1"},
            "start_times": ("s1",),
            "finish_times": ("f1",),
            "pids": (123,),
            "durations": (1.25,),
            "exit_codes": (0,),
            "source_states": ("git abc | clean | script abc",),
            "records": [{"loss": 0.1}],
            "tracks": [{"step": 1}],
        },
        summary=True,
    )
    assert summary["dir"] == "C:/tmp/task"
    assert summary["durations"] == [1.25]
    assert summary["exit_codes"] == [0]
    assert summary["source_states"] == ["git abc | clean | script abc"]
    assert summary["config"] == {}
    assert summary["records"] == []
    assert summary["tracks"] == []
    assert summary["env"] == {"A": "1"}


def test_task_manager_api_snapshots_stay_consistent_during_locked_gpu_updates(tmp_path, monkeypatch):
    manager = TaskManager(
        tasks_dir=str(tmp_path),
        lazy_scan=None,
        owns_task_lifecycle=False,
    )
    live_task = {
        "dir": str(tmp_path / "snapshot-race"),
        "name": "snapshot-race",
        "status": "queued",
        "progress": 0,
        "gpu_wait": {"generation": 0, "started_at": 0.0},
    }
    with manager._lock:
        manager.tasks = [live_task]
        manager._rebuild_indexes_locked()

    original_serialize_wait = TaskManager._serialized_gpu_wait
    sync: dict[str, threading.Event] = {}

    def pause_before_gpu_wait_copy(task):
        sync["entered"].set()
        if not sync["updated"].wait(2):
            raise AssertionError("GPU update did not complete while the API snapshot was serialized")
        return original_serialize_wait(task)

    monkeypatch.setattr(
        TaskManager,
        "_serialized_gpu_wait",
        staticmethod(pause_before_gpu_wait_copy),
    )

    def capture(call):
        with manager._lock:
            live_task["progress"] = 0
            live_task["gpu_wait"] = {"generation": 0, "started_at": 0.0}

        sync["entered"] = threading.Event()
        sync["updated"] = threading.Event()
        writer_errors: list[str] = []

        def update_gpu_wait():
            if not sync["entered"].wait(2):
                writer_errors.append("API serialization did not reach the GPU wait field")
                sync["updated"].set()
                return
            with manager._lock:
                live_task["progress"] = 1
                live_task["gpu_wait"] = {"generation": 1, "started_at": 0.0}
            sync["updated"].set()

        writer = threading.Thread(target=update_gpu_wait)
        writer.start()
        snapshot = call()
        writer.join(timeout=2)

        assert not writer.is_alive()
        assert writer_errors == []
        assert snapshot["progress"] == 0
        assert snapshot["gpu_wait"]["generation"] == 0

    capture(lambda: manager.list_tasks(summary=True)[0])
    capture(lambda: manager.get_task_summary_page()[0][0])
    capture(lambda: manager.get_task("snapshot-race"))


@pytest.mark.parametrize("sort_mode", [
    "priority", "manual", "activity_desc", "activity_asc", "name_asc", "name_desc",
])
@pytest.mark.parametrize("summary", [False, True])
def test_task_page_preserves_filter_order_counts_and_detachment(tmp_path, sort_mode, summary):
    from pyruns.utils.sort_utils import filter_tasks, sort_tasks_for_manager

    manager = TaskManager(tasks_dir=str(tmp_path), lazy_scan=None, owns_task_lifecycle=False)
    manager.tasks = [
        {
            "name": name, "status": status, "pinned": pinned, "task_order": order,
            "created_at": "2026-09-08T00:00:00", "notes": "keep draft",
            "search_text": "", "env": {"CUDA_VISIBLE_DEVICES": "0"},
            "start_times": ["2026-09-07T00:00:00", f"2026-09-08T00:0{index}:00"],
            "finish_times": (), "config": {"hidden_in_summary": True},
        }
        for index, (name, status, pinned, order) in enumerate([
            ("task10", "pending", False, None), ("task2", "running", False, 2),
            ("pinned", "completed", True, 0), ("failed", "failed", False, 1),
            ("queued", "queued", True, None),
        ])
    ]
    baseline = manager.list_tasks(summary=summary)
    for query, status, offset, limit in [
        ("", "All", 1, 2), ("keep\ntask", "All", 0, 1),
        ("", "running", 0, 0), ("hidden_in_summary", "All", 0, 5),
        ("", "All", 99, 5), ("", "All", 0, 0),
    ]:
        expected = sort_tasks_for_manager(filter_tasks(baseline, query, status), sort_mode)
        with patch.object(manager, "_snapshot_task_for_api", wraps=manager._snapshot_task_for_api) as copy_task:
            page_method = manager.get_task_summary_page if summary else manager.get_task_page
            items, total, counts = page_method(
                query=query, status=status, offset=offset, limit=limit, sort_mode=sort_mode,
            )
        assert items == (expected[offset:offset + limit] if limit else expected[offset:])
        assert total == len(expected)
        assert counts == {"pending": 1, "queued": 1, "running": 1, "completed": 1, "failed": 1, "cancelled": 0}
        assert copy_task.call_count == len(items)
        if items:
            items[0]["env"]["CUDA_VISIBLE_DEVICES"] = "changed"
            items[0]["start_times"].clear()
            assert all(task["env"]["CUDA_VISIBLE_DEVICES"] == "0" and task["start_times"] for task in manager.tasks)


@pytest.mark.parametrize("summary", [False, True])
def test_task_page_preserves_tuple_activity_order(tmp_path, summary):
    manager = TaskManager(tasks_dir=str(tmp_path), lazy_scan=None, owns_task_lifecycle=False)
    manager.tasks = [
        {"name": "latest-start", "start_times": ["2026-09-25T12:00:00"]},
        {"name": "latest-finish", "finish_times": ["2026-09-25T13:00:00"]},
        {"name": "created", "created_at": "2026-09-25T11:00:00"},
    ]
    for sort_mode in ("priority", "manual", "activity_asc", "activity_desc"):
        list_page = manager.get_task_page(sort_mode=sort_mode, summary=summary)
        for task in manager.tasks:
            for field in ("start_times", "finish_times"):
                if field in task:
                    task[field] = tuple(task[field])
        tuple_page = manager.get_task_page(sort_mode=sort_mode, summary=summary)
        assert [task["name"] for task in tuple_page[0]] == [task["name"] for task in list_page[0]]
        assert tuple_page[1:] == list_page[1:]
        for task in manager.tasks:
            for field in ("start_times", "finish_times"):
                if field in task:
                    task[field] = list(task[field])


@pytest.mark.parametrize("summary", [False, True])
@pytest.mark.parametrize("query,search_field,status,expected", [
    ("config_token", "all", "All", ["train1"]),
    ("script_token", "all", "All", ["shell2"]),
    ("cached_token", "all", "All", ["cached3"]),
    ("config_token", "config", "All", ["cached3", "train1"]),
    ("script_token", "script", "All", ["shell2"]),
    ("train", "name", "All", ["train1"]),
    ("note_token", "notes", "All", ["train1"]),
    ("TOKEN=env_value", "env", "All", ["cached3", "train1"]),
    ("note_token\nTOKEN=env_value", "all", "All", ["train1"]),
    ("", "all", "pending", ["train1"]),
    ("train", "all", "PENDING", ["train1"]),
])
def test_task_page_preserves_search_sources(tmp_path, summary, query, search_field, status, expected):
    manager = TaskManager(tasks_dir=str(tmp_path), lazy_scan=None, owns_task_lifecycle=False)
    manager.tasks = [
        {
            "name": "train1", "notes": "note_token", "env": {"TOKEN": "env_value"},
            "task_kind": TASK_KIND_CONFIG, "config": {"config_token": 1},
        },
        {
            "name": "shell2", "status": "running", "task_kind": TASK_KIND_SHELL,
            "config_text": "echo script_token",
        },
        {
            "name": "cached3", "status": "completed", "search_text": "cached_token",
            "config": {"config_token": 2}, "env": {"TOKEN": "env_value"},
        },
        None,
    ]
    if summary and search_field == "all" and query in {"config_token", "script_token"}:
        expected = []
    items, total, counts = manager.get_task_page(
        query=query, search_field=search_field, status=status, sort_mode="name_asc", summary=summary,
    )
    assert [task["name"] for task in items] == expected
    assert total == len(expected)
    assert counts == {"pending": 1, "queued": 0, "running": 1, "completed": 1, "failed": 0, "cancelled": 0}
    assert "status" not in manager.tasks[0]
    assert "search_text" not in manager.tasks[0]


def test_task_manager_scan_and_load_task_dir_edge_cases(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    missing_info_dir = tasks_dir / "missing-info"
    missing_info_dir.mkdir()

    manager = _make_task_manager(tasks_dir)

    assert manager._load_task_dir("missing-info") is None

    empty_dir = tasks_dir / "empty-info"
    empty_dir.mkdir()
    (empty_dir / TASK_INFO_FILENAME).write_text("{}", encoding="utf-8")
    with patch("pyruns.core.task_manager.load_task_metadata", return_value={}):
        empty_task = manager._load_task_dir("empty-info")
    assert empty_task is not None
    assert empty_task["status"] == "failed"
    assert "metadata is empty" in empty_task["_load_error"].lower()

    with patch("pyruns.core.task_manager.load_task_metadata", side_effect=RuntimeError("bad info")):
        broken_task = manager._load_task_dir("empty-info")
    assert broken_task is not None
    assert broken_task["status"] == "failed"
    assert "bad info" in broken_task["_load_error"]

    statless_dir = tasks_dir / "statless"
    statless_dir.mkdir()
    save_task_info(
        str(statless_dir),
        {
            "name": "statless",
            "status": "pending",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 2,
        },
    )
    save_yaml(str(statless_dir / CONFIG_FILENAME), {"lr": 0.01})

    original_exists = os.path.exists
    original_stat = os.stat
    info_path = str(statless_dir / TASK_INFO_FILENAME)

    def fake_exists(path):
        if str(path) == info_path:
            return True
        return original_exists(path)

    def fake_stat(path, *args, **kwargs):
        if str(path).endswith(TASK_INFO_FILENAME):
            raise OSError("no stat")
        return original_stat(path, *args, **kwargs)

    with (
        patch("pyruns.core.task_manager.os.path.exists", side_effect=fake_exists),
        patch("pyruns.core.task_manager.os.stat", side_effect=fake_stat),
    ):
        loaded = manager._load_task_dir("statless")
    assert loaded["_mtime_ns"] == 0
    assert loaded["run_index"] == 2

    with patch("pyruns.core.task_manager.os.scandir", side_effect=OSError("scandir failed")):
        manager.scan_disk()
    assert manager.list_tasks() == []


def test_task_manager_refresh_discovers_external_added_and_removed_tasks(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    alpha = generator.create_task("alpha", {"value": 1})
    retained = generator.create_task("retained", {"value": 3})

    manager = _make_task_manager(tasks_dir)

    generator.create_task("beta", {"value": 2})
    shutil.rmtree(alpha["dir"])
    Path(retained["dir"], CONFIG_FILENAME).write_text("value: 4\n", encoding="utf-8")
    update_task_info(retained["dir"], lambda info: info.update(status="queued"))

    with patch.object(manager, "_probe_refresh_task", wraps=manager._probe_refresh_task) as probe:
        assert manager.refresh_from_disk(check_all=True, discover=True) is True
    assert [call.args[0]["name"] for call in probe.call_args_list] == ["retained"]

    tasks = {task["name"]: task for task in manager.list_tasks()}
    assert set(tasks) == {"beta", "retained"}
    assert tasks["beta"]["config"]["value"] == 2
    assert tasks["retained"]["config"]["value"] == 4
    assert manager.is_processing is True

    update_task_info(retained["dir"], lambda info: info.update(status="completed"))
    assert manager.refresh_from_disk(check_all=True) is True
    assert manager.is_processing is False


def test_task_manager_refreshes_edited_payload_and_clears_parse_error(tmp_path):
    task_dir = tmp_path / "sample"
    task_dir.mkdir()
    save_task_info(str(task_dir), {"status": "pending", "task_kind": TASK_KIND_CONFIG})
    config_path = task_dir / CONFIG_FILENAME
    config_path.write_text("epochs: 1\n", encoding="utf-8")
    manager = _make_task_manager(tmp_path)
    info_mtime = (task_dir / TASK_INFO_FILENAME).stat().st_mtime_ns

    config_path.write_text("epochs: [broken\n", encoding="utf-8")
    with patch.object(manager, "_payload_signature", wraps=manager._payload_signature) as probe:
        assert manager.refresh_from_disk(check_all=True, check_payload=False) is False
        probe.assert_not_called()
    assert manager.get_task("sample")["config"]["epochs"] == 1
    assert manager.refresh_from_disk(task_ids=["sample"]) is True
    assert manager.get_task("sample")["_load_error"]
    with patch("pyruns.utils.task_files.load_config_view_text") as parse:
        assert manager.refresh_from_disk(task_ids=["sample"]) is False
        parse.assert_not_called()

    config_path.write_text("epochs: 2\n", encoding="utf-8")
    assert manager.refresh_from_disk(task_ids=["sample"]) is True
    task = manager.get_task("sample")
    assert task["_load_error"] == ""
    assert task["config"]["epochs"] == 2
    assert manager.get_task_summary_page(query="epochs:2", search_field="config")[1] == 1
    assert (task_dir / TASK_INFO_FILENAME).stat().st_mtime_ns == info_mtime


def test_task_manager_refreshes_replaced_metadata_with_same_mtime_and_size(tmp_path):
    task_dir = tmp_path / "sample"
    task_dir.mkdir()
    save_task_info(str(task_dir), {"status": "pending", "notes": "before"})
    (task_dir / CONFIG_FILENAME).write_text("epochs: 1\n", encoding="utf-8")
    manager = _make_task_manager(tmp_path)
    assert manager.get_task("sample")["notes"] == "before"

    info_path = task_dir / TASK_INFO_FILENAME
    original = info_path.stat()
    replacement = tmp_path / "replacement.json"
    original_text = info_path.read_text(encoding="utf-8")
    assert "before" in original_text
    replacement.write_text(original_text.replace("before", "after!"), encoding="utf-8")
    assert replacement.stat().st_size == original.st_size
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    replacement.replace(info_path)

    assert manager.refresh_from_disk(check_all=True) is True
    assert manager.get_task("sample")["notes"] == "after!"


def test_task_manager_rechecks_metadata_replaced_during_initial_load(tmp_path):
    import pyruns.core.task_manager as task_manager_module

    task_dir = tmp_path / "sample"
    task_dir.mkdir()
    save_task_info(str(task_dir), {"status": "pending", "notes": "before"})
    (task_dir / CONFIG_FILENAME).write_text("epochs: 1\n", encoding="utf-8")
    replacement = tmp_path / "replacement.json"
    info_path = task_dir / TASK_INFO_FILENAME
    replacement.write_text(
        info_path.read_text(encoding="utf-8").replace("before", "after!"),
        encoding="utf-8",
    )
    manager = _make_task_manager(tmp_path, lazy_scan=None)
    original_load = task_manager_module.load_task_metadata

    def replace_after_read(*args, **kwargs):
        info = original_load(*args, **kwargs)
        if replacement.exists():
            replacement.replace(info_path)
        return info

    with patch.object(task_manager_module, "load_task_metadata", side_effect=replace_after_read):
        assert manager.load_task_by_name("sample")["notes"] == "before"
    assert manager.refresh_from_disk(task_ids=["sample"]) is True
    assert manager.get_task("sample")["notes"] == "after!"


@pytest.mark.parametrize("failure", ["corrupt", "initial_unavailable", "refresh_unavailable"])
def test_task_manager_reports_metadata_errors_and_recovers(tmp_path, failure):
    task_dir = tmp_path / "sample"
    task_dir.mkdir()
    info = {"status": "pending", "notes": "keep"}
    save_task_info(str(task_dir), info)
    (task_dir / CONFIG_FILENAME).write_text("epochs: 1\n", encoding="utf-8")
    manager = _make_task_manager(tmp_path, lazy_scan=None)
    info_path = task_dir / TASK_INFO_FILENAME

    if failure == "initial_unavailable":
        with patch("pyruns.core.task_manager.load_task_metadata", side_effect=PermissionError("temporarily unavailable")):
            manager.scan_disk()
    else:
        manager.scan_disk()
        if failure == "refresh_unavailable":
            with patch("pyruns.core.task_manager.load_task_metadata", side_effect=PermissionError("temporarily unavailable")):
                assert manager.refresh_from_disk(force_all=True, check_payload=False) is True
        else:
            info_path.write_text("{broken", encoding="utf-8")
            assert manager.refresh_from_disk(check_all=True, check_payload=False) is True
    assert "Could not load task metadata" in manager.get_task("sample")["_load_error"]

    if failure == "corrupt":
        save_task_info(str(task_dir), info)
    assert manager.refresh_from_disk(check_all=True, check_payload=False) is True
    assert manager.get_task("sample")["_load_error"] == ""
    assert manager.get_task("sample")["notes"] == "keep"


def test_task_manager_refreshes_edited_shell_payload(tmp_path):
    task_dir = tmp_path / "shell-task"
    task_dir.mkdir()
    save_task_info(str(task_dir), {
        "status": "pending", "task_kind": TASK_KIND_SHELL,
        "config_file": SHELL_CONFIG_FILENAME,
    })
    script_path = task_dir / SHELL_CONFIG_FILENAME
    script_path.write_text("echo before\n", encoding="utf-8", newline="\n")
    manager = _make_task_manager(tmp_path)

    script_path.write_text("echo after\n", encoding="utf-8", newline="\n")
    assert manager.refresh_from_disk(task_ids=["shell-task"]) is True
    assert manager.get_task("shell-task")["config_text"] == "echo after\n"
    assert manager.get_task_summary_page(query="after", search_field="script")[1] == 1


@pytest.mark.parametrize("read_mode", ["load", "refresh"])
def test_task_manager_discovers_implicit_shell_payload_after_creation(tmp_path, monkeypatch, read_mode):
    task_dir = tmp_path / "shell-task"
    task_dir.mkdir()
    save_task_info(str(task_dir), {"status": "pending", "task_kind": TASK_KIND_SHELL})
    manager = _make_task_manager(tmp_path)
    assert manager.get_task("shell-task")["_load_error"]

    (task_dir / POWERSHELL_CONFIG_FILENAME).write_text("Write-Output ready\n", encoding="utf-8", newline="\n")
    assert manager.refresh_from_disk(check_all=True) is True
    task = manager.get_task("shell-task")
    assert task["config_file"] == POWERSHELL_CONFIG_FILENAME
    assert task["config_text"] == "Write-Output ready\n"
    assert task["_load_error"] == ""

    read_payload = task_manager_module.read_task_payload_snapshot

    def read_after_preferred_file_appears(*args, **kwargs):
        (task_dir / SHELL_CONFIG_FILENAME).write_text("echo preferred\n", encoding="utf-8", newline="\n")
        return read_payload(*args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(task_manager_module, "read_task_payload_snapshot", read_after_preferred_file_appears)
        if read_mode == "load":
            manager.load_task_by_name("shell-task")
        else:
            manager.refresh_from_disk(force_all=True)
    task = manager.get_task("shell-task")
    assert task["config_file"] == POWERSHELL_CONFIG_FILENAME
    assert task["config_text"] == "Write-Output ready\n"

    assert manager.refresh_from_disk(check_all=True) is True
    task = manager.get_task("shell-task")
    assert task["config_file"] == SHELL_CONFIG_FILENAME
    assert task["config_text"] == "echo preferred\n"


def test_task_manager_add_tasks_upserts_existing_name(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    alpha = generator.create_task("alpha", {"value": 1})

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        manager._tasks_by_name["alpha"]["script"] = "train.py"

    duplicate = dict(alpha)
    duplicate["notes"] = "created through api"

    manager.add_tasks([duplicate])

    tasks = [task for task in manager.list_tasks() if task["name"] == "alpha"]
    assert len(tasks) == 1
    assert tasks[0]["notes"] == "created through api"
    assert tasks[0]["script"] == "train.py"


def test_task_manager_batch_merge_preserves_order_worker_references_and_fields(tmp_path):
    manager = TaskManager(str(tmp_path), lazy_scan=None, owns_task_lifecycle=False)
    manager.add_tasks([
        {"name": "alpha", "status": "running", "runner_id": "owner"},
        {"name": "beta", "status": "pending", "script": "train.py"},
        {"name": "untouched", "status": "completed"},
    ])
    original_alpha = manager._tasks_by_name["alpha"]
    original_beta = manager._tasks_by_name["beta"]
    revisions = {name: task["_registry_revision"] for name, task in manager._tasks_by_name.items()}
    notifications = []
    manager.on_change(lambda: notifications.append([task["name"] for task in manager.tasks]))

    manager.add_tasks([
        {"name": "beta", "notes": "first occurrence wins"},
        {"name": "new", "notes": "new first"},
        {"name": "alpha", "progress": 0.5},
        {"name": "beta", "notes": "later occurrence", "env": {"SEED": "7"}},
        {"name": "new", "script": "new.py"},
    ])

    assert notifications == [["beta", "new", "alpha", "untouched"]]
    assert manager._tasks_by_name["alpha"] is original_alpha
    assert manager._tasks_by_name["beta"] is original_beta
    assert original_alpha["status"] == "running"
    assert original_alpha["runner_id"] == "owner"
    assert original_alpha["progress"] == 0.5
    assert original_alpha["_registry_revision"] == revisions["alpha"] + 1
    assert original_beta["script"] == "train.py"
    assert original_beta["env"] == {"SEED": "7"}
    assert original_beta["notes"] == "first occurrence wins"
    assert original_beta["_registry_revision"] == revisions["beta"] + 2
    assert manager._tasks_by_name["new"]["notes"] == "new first"
    assert manager._tasks_by_name["new"]["script"] == "new.py"

    manager.add_task({"name": "alpha", "progress": 0.75})
    assert [task["name"] for task in manager.tasks] == ["alpha", "beta", "new", "untouched"]
    assert manager._tasks_by_name["alpha"] is original_alpha
    assert original_alpha["progress"] == 0.75
    manager.shutdown()


def test_task_manager_refresh_keeps_discovered_tasks_in_disk_order(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    newest = generator.create_task("newest", {"value": 3})
    base = time.time()
    os.utime(newest["dir"], (base + 30, base + 30))

    manager = _make_task_manager(tasks_dir)

    older = generator.create_task("older", {"value": 1})
    middle = generator.create_task("middle", {"value": 2})
    os.utime(older["dir"], (base + 10, base + 10))
    os.utime(middle["dir"], (base + 20, base + 20))

    assert manager.refresh_from_disk(check_all=True, discover=True) is True

    assert [task["name"] for task in manager.list_tasks()] == ["newest", "middle", "older"]


def test_task_manager_discovery_does_not_remove_recreated_task(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    original = generator.create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    shutil.rmtree(original["dir"])
    scan = manager._scan_task_dir_names

    def scan_then_recreate(*args, **kwargs):
        result = scan(*args, **kwargs)
        generator.create_task("alpha", {"value": 2})
        manager.add_task(manager._load_task_dir("alpha"))
        return result

    monkeypatch.setattr(manager, "_scan_task_dir_names", scan_then_recreate)
    manager.sync_task_dirs_from_disk()

    assert manager.get_task("alpha")["config"]["value"] == 2


def test_task_manager_full_scan_keeps_task_added_while_loading(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    generator.create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, lazy_scan=None, owns_task_lifecycle=False)
    load_dirs = manager._load_task_dirs
    added = False

    def load_then_add(names, **kwargs):
        nonlocal added
        loaded = load_dirs(names, **kwargs)
        if not added:
            added = True
            generator.create_task("beta", {"value": 2})
            manager.add_task(manager._load_task_dir("beta"))
        return loaded

    monkeypatch.setattr(manager, "_load_task_dirs", load_then_add)
    manager.scan_disk()

    assert {task["name"] for task in manager.list_tasks()} == {"alpha", "beta"}


def test_task_manager_full_scan_keeps_task_reloaded_while_loading(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    load_dirs = manager._load_task_dirs
    reloaded = False

    def load_then_reload(names, **kwargs):
        nonlocal reloaded
        loaded = load_dirs(names, **kwargs)
        if not reloaded:
            reloaded = True
            save_yaml(str(Path(task["dir"]) / CONFIG_FILENAME), {"value": 2})
            manager.load_task_by_name("alpha")
        return loaded

    monkeypatch.setattr(manager, "_load_task_dirs", load_then_reload)
    manager.scan_disk()

    assert manager.get_task("alpha")["config"]["value"] == 2


def test_task_manager_full_scan_reconciles_edited_task_after_concurrent_add(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    alpha = generator.create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    load_dirs = manager._load_task_dirs
    added = False

    def load_then_edit(names, **kwargs):
        nonlocal added
        loaded = load_dirs(names, **kwargs)
        if not added:
            added = True
            save_yaml(str(Path(alpha["dir"]) / CONFIG_FILENAME), {"value": 2})
            generator.create_task("beta", {"value": 3})
            manager.add_task(manager._load_task_dir("beta"))
        return loaded

    monkeypatch.setattr(manager, "_load_task_dirs", load_then_edit)
    manager.scan_disk()

    assert manager.get_task("alpha")["config"]["value"] == 2
    assert manager.get_task("beta")["config"]["value"] == 3


def test_task_manager_parallel_directory_scan_keeps_order_and_skips_unsafe_paths(
    tmp_path, monkeypatch, simulate_reparse,
):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    base_time = time.time() - 100
    for index in range(12):
        task = generator.create_task(f"task-{index}", {"value": index})
        os.utime(task["dir"], (base_time + index, base_time + index))
    hidden = tasks_dir / ".internal"
    hidden.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (tasks_dir / "unsafe").symlink_to(outside, target_is_directory=True)
    except OSError:
        pass
    reparse = tasks_dir / "reparse"
    reparse.mkdir()
    simulate_reparse(reparse)
    manager = _make_task_manager(tasks_dir, lazy_scan=None, owns_task_lifecycle=False)
    validate = task_manager_module.validate_workspace_directory
    worker_threads = []
    main_thread = threading.get_ident()

    def observed_validate(path):
        worker_threads.append(threading.get_ident())
        return validate(path)

    monkeypatch.setattr(task_manager_module, "validate_workspace_directory", observed_validate)
    monkeypatch.setattr(manager, "_parallel_loading_worthwhile", lambda *_: True)

    ok, names = manager._scan_task_dir_names()

    assert ok is True
    assert names == [f"task-{index}" for index in reversed(range(12))]
    assert any(thread != main_thread for thread in worker_threads)


def test_task_manager_refresh_does_not_overwrite_replaced_task(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    update_task_info(task["dir"], lambda info: info.update({"status": "failed"}))
    original_load = task_manager_module.load_task_metadata

    def load_then_replace(task_dir, **kwargs):
        stale_info = original_load(task_dir, **kwargs)
        update_task_info(task_dir, lambda info: info.update({"status": "completed"}))
        with manager._lock:
            replacement = dict(manager._tasks_by_name["alpha"])
        replacement["status"] = "completed"
        manager.add_task(replacement)
        return stale_info

    monkeypatch.setattr(task_manager_module, "load_task_metadata", load_then_replace)
    manager.refresh_from_disk(check_all=True)

    assert manager.get_task("alpha")["status"] == "completed"


def test_task_manager_refresh_does_not_overwrite_newer_in_place_update(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    update_task_info(task["dir"], lambda info: info.update({"status": "failed"}))
    original_load = task_manager_module.load_task_metadata

    def load_then_refresh(task_dir, **kwargs):
        stale_info = original_load(task_dir, **kwargs)
        update_task_info(task_dir, lambda info: info.update({"status": "completed"}))
        with manager._lock:
            manager._apply_info_to_task(
                manager._tasks_by_name["alpha"], original_load(task_dir),
            )
        return stale_info

    monkeypatch.setattr(task_manager_module, "load_task_metadata", load_then_refresh)
    manager.refresh_from_disk(check_all=True)

    assert manager.get_task("alpha")["status"] == "completed"


def test_task_manager_exact_load_preserves_concurrent_update(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    original_load = manager._load_task_dir

    def load_then_update(name, **kwargs):
        stale_task = original_load(name, **kwargs)
        updated = update_task_info(task["dir"], lambda info: info.update({"notes": "latest"}))
        with manager._lock:
            manager._apply_info_to_task(manager._tasks_by_name[name], updated)
        return stale_task

    monkeypatch.setattr(manager, "_load_task_dir", load_then_update)
    loaded = manager.load_task_by_name("alpha")

    assert loaded["notes"] == "latest"
    assert manager.get_task("alpha")["notes"] == "latest"


@pytest.mark.parametrize("reload_method", ["load_task_by_name", "scan_disk"])
def test_task_manager_reload_keeps_independent_gpu_queue(tmp_path, reload_method):
    tasks_dir = tmp_path / "tasks"
    TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    manager._sync_status_to_disk("alpha", "queued", run_index=1, counts_for_batch=False)
    with manager._lock:
        manager._tasks_by_name["alpha"]["_queued_independent"] = True

    if reload_method == "scan_disk":
        manager.scan_disk()
    else:
        manager.load_task_by_name("alpha")

    picked, run_index = manager._pick_queued_task(independent_only=True)
    assert picked is not None
    assert picked["name"] == "alpha"
    assert run_index == 1
    assert "alpha" not in manager._batch_running_ids


def test_task_manager_refresh_preserves_task_picked_during_read(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    manager._sync_status_to_disk("alpha", "queued", run_index=1)
    original_load = task_manager_module.load_task_metadata

    def load_then_pick(task_dir, **kwargs):
        stale_info = original_load(task_dir, **kwargs)
        picked, run_index = manager._pick_queued_task()
        assert picked["name"] == "alpha"
        assert run_index == 1
        return stale_info

    monkeypatch.setattr(task_manager_module, "load_task_metadata", load_then_pick)
    manager.refresh_from_disk(force_all=True)

    assert manager.get_task("alpha")["status"] == "running"
    assert "alpha" in manager._batch_running_ids


def test_task_manager_full_scan_preserves_task_picked_during_read(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    manager._sync_status_to_disk("alpha", "queued", run_index=1)
    original_load = manager._load_task_dirs

    def load_then_pick(names, **kwargs):
        loaded = original_load(names, **kwargs)
        picked, _ = manager._pick_queued_task()
        assert picked["name"] == "alpha"
        return loaded

    monkeypatch.setattr(manager, "_load_task_dirs", load_then_pick)
    manager.scan_disk()

    assert manager.get_task("alpha")["status"] == "running"
    assert "alpha" in manager._batch_running_ids


def test_task_manager_refresh_skips_metadata_replaced_during_read(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    update_task_info(task["dir"], lambda info: info.update({"status": "failed"}))
    original_load = task_manager_module.load_task_metadata

    def load_then_replace(task_dir, **kwargs):
        stale_info = original_load(task_dir, **kwargs)
        update_task_info(task_dir, lambda info: info.update({"status": "completed"}))
        return stale_info

    monkeypatch.setattr(task_manager_module, "load_task_metadata", load_then_replace)
    manager.refresh_from_disk(check_all=True)
    assert manager.get_task("alpha")["status"] == "pending"

    monkeypatch.setattr(task_manager_module, "load_task_metadata", original_load)
    manager.refresh_from_disk(check_all=True)
    assert manager.get_task("alpha")["status"] == "completed"


def test_task_manager_refresh_detects_disk_write_after_local_update(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    original_update = task_manager_module.update_task_metadata

    def update_then_external_edit(task_dir, mutator, **kwargs):
        local_info = original_update(task_dir, mutator, **kwargs)
        original_update(task_dir, lambda info: info.update({"notes": "external latest"}))
        return local_info

    monkeypatch.setattr(task_manager_module, "update_task_metadata", update_then_external_edit)
    assert manager.update_task_notes("alpha", "local edit", expected_notes="")[0]
    manager.refresh_from_disk(task_ids=["alpha"])

    assert manager.get_task("alpha")["notes"] == "external latest"


def test_task_manager_parallel_refresh_preserves_order_and_error_semantics(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    created = [generator.create_task(f"task-{index}", {"value": index}) for index in range(12)]
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    original_order = [task["name"] for task in manager.list_tasks()]
    update_task_info(created[0]["dir"], lambda info: info.update({"status": "failed"}))
    (Path(created[5]["dir"]) / TASK_INFO_FILENAME).unlink()
    probe = manager._probe_refresh_task
    probe_threads = []
    main_thread = threading.get_ident()

    def observed_probe(task, *, check_payload):
        probe_threads.append(threading.get_ident())
        return probe(task, check_payload=check_payload)

    monkeypatch.setattr(manager, "_probe_refresh_task", observed_probe)
    monkeypatch.setattr(manager, "_parallel_loading_worthwhile", lambda *_: True)

    with pytest.raises(FileNotFoundError):
        manager.refresh_from_disk(check_all=True, raise_on_error=True)
    assert manager.get_task("task-5") is not None

    assert manager.refresh_from_disk(check_all=True) is True
    assert [task["name"] for task in manager.list_tasks()] == [
        name for name in original_order if name != "task-5"
    ]
    assert manager.get_task("task-0")["status"] == "failed"
    assert any(thread != main_thread for thread in probe_threads)


@pytest.mark.parametrize("samples,parallel", [
    pytest.param([(0.020, 0.019)] * 3, False, id="cpu-bound"),
    pytest.param([(0.008689, 0.000459), (0.0004, 0.0004), (0.0004, 0.0004)], False, id="one-io-stall"),
    pytest.param([(0.020, 0.001)] * 3, True, id="sustained-io"),
])
def test_task_manager_loads_many_task_dirs_in_order(tmp_path, monkeypatch, samples, parallel):
    manager = _make_task_manager(tmp_path, lazy_scan=None, owns_task_lifecycle=False)
    names = [f"task-{index}" for index in range(12)]
    loader_threads = []
    main_thread = threading.get_ident()

    def load(name, **_kwargs):
        loader_threads.append(threading.get_ident())
        return None if name == "task-4" else {"name": name}

    monkeypatch.setattr(manager, "_load_task_dir", load)
    clock = MagicMock(wraps=task_manager_module.time)
    clock.perf_counter.side_effect = [value for wall, _ in samples for value in (0.0, wall)]
    clock.thread_time.side_effect = [value for _, cpu in samples for value in (0.0, cpu)]
    monkeypatch.setattr(task_manager_module, "time", clock)

    loaded = manager._load_task_dirs(names)

    assert [task["name"] for task in loaded] == [name for name in names if name != "task-4"]
    assert any(thread != main_thread for thread in loader_threads) is parallel


def test_task_manager_strict_discovery_keeps_state_on_parallel_load_error(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    broken = tasks_dir / "broken"
    broken.mkdir()
    (broken / TASK_INFO_FILENAME).write_text("not json", encoding="utf-8")
    old_time = time.time() - 100
    os.utime(broken, (old_time, old_time))
    generator = TaskGenerator(root_dir=str(tasks_dir))
    for index in range(9):
        generator.create_task(f"task-{index}", {"value": index})
    manager = _make_task_manager(tasks_dir, lazy_scan=None, owns_task_lifecycle=False)
    monkeypatch.setattr(manager, "_parallel_loading_worthwhile", lambda *_: True)

    with pytest.raises(ValueError):
        manager.sync_task_dirs_from_disk(raise_on_error=True)

    assert manager.list_tasks() == []


def test_task_manager_refresh_keeps_tasks_when_directory_scan_fails(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    generator.create_task("alpha", {"value": 1})

    manager = _make_task_manager(tasks_dir)

    with patch("pyruns.core.task_manager.os.scandir", side_effect=OSError("stale nfs handle")):
        assert manager.refresh_from_disk(check_all=True, discover=True) is False

    assert [task["name"] for task in manager.list_tasks()] == ["alpha"]


def test_task_manager_strict_refresh_fails_closed_on_disk_errors(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    for name in ("alpha", "beta", "gamma"):
        generator.create_task(name, {"value": 1})

    manager = _make_task_manager(tasks_dir)
    manager.is_processing = True
    with patch.object(manager, "_recompute_processing_flag_locked", wraps=manager._recompute_processing_flag_locked) as check:
        manager.refresh_from_disk(force_all=True, discover=True, raise_on_error=True)
    assert check.call_count <= 1
    assert manager.is_processing is False

    middle = manager.tasks[1]
    manager._sync_status_to_disk(middle["name"], "queued", run_index=1)
    update_task_info(middle["dir"], lambda info: info.update(status="completed"))
    apply_info = manager._apply_info_to_task

    def apply_then_fail(task, info, **kwargs):
        apply_info(task, info, **kwargs)
        if task["name"] == middle["name"]:
            raise OSError("error after applying metadata")

    with patch.object(manager, "_apply_info_to_task", side_effect=apply_then_fail):
        manager.refresh_from_disk(force_all=True)
    assert manager.get_task(middle["name"])["status"] == "completed"
    assert manager.is_processing is False

    with patch("pyruns.core.task_manager.os.scandir", side_effect=OSError("stale nfs handle")):
        with pytest.raises(OSError, match="stale nfs handle"):
            manager.refresh_from_disk(
                force_all=True,
                discover=True,
                raise_on_error=True,
            )

    with patch(
        "pyruns.core.task_manager.load_task_metadata",
        side_effect=OSError("task metadata unavailable"),
    ):
        with pytest.raises(OSError, match="task metadata unavailable"):
            manager.refresh_from_disk(
                force_all=True,
                discover=True,
                raise_on_error=True,
            )

    broken_dir = tasks_dir / "broken"
    broken_dir.mkdir()
    (broken_dir / TASK_INFO_FILENAME).write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        manager.refresh_from_disk(
            force_all=True,
            discover=True,
            raise_on_error=True,
        )

    (broken_dir / TASK_INFO_FILENAME).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="metadata status is missing"):
        manager.refresh_from_disk(
            force_all=True,
            discover=True,
            raise_on_error=True,
        )

    (broken_dir / TASK_INFO_FILENAME).write_text(
        json.dumps({"status": "unknown"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="metadata status is invalid"):
        manager.refresh_from_disk(
            force_all=True,
            discover=True,
            raise_on_error=True,
        )


def test_task_manager_pin_reorder_notes_env_and_rename_edges(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    alpha = generator.create_task("alpha", {"value": 1})
    beta = generator.create_task("beta", {"value": 2})

    manager = _make_task_manager(tasks_dir)

    assert manager.set_task_pinned("missing") == (False, "Task not found")
    ok, pinned = manager.set_task_pinned("alpha")
    assert ok is True and pinned is True

    assert manager.reorder_tasks([])[1] == "No valid tasks were provided for reordering."
    assert manager.reorder_tasks([{"name": "alpha"}, {"name": "alpha"}])[1].startswith("Duplicate task")
    assert manager.reorder_tasks([{"name": "missing"}])[1] == "Task not found: missing"
    ok, reordered = manager.reorder_tasks([{"name": "beta", "pinned": True}, {"name": "alpha", "pinned": False}])
    assert ok is True
    assert [item["name"] for item in reordered] == ["beta", "alpha"]
    assert manager.get_task("beta")["pinned"] is True

    assert manager.update_task_notes("missing", "x", "") == (False, "Task not found")
    assert manager.update_task_notes("alpha", "note", "") == (True, "note")
    assert manager.update_task_env("missing", {}, {}) == (False, "Task not found")
    assert manager.update_task_env("alpha", {"A": 1}, {}) == (True, {"A": "1"})
    with pytest.raises(TaskStateConflict, match="environment changed"):
        manager.update_task_env("alpha", {"B": "2"}, {})
    assert manager.update_task_env("alpha", {"B": "2"}, {"A": "1"}) == (True, {"B": "2"})
    assert manager.update_task_env("alpha", {"BAD=KEY": "x"}, {"B": "2"}) == (
        False,
        "invalid environment variable name: BAD=KEY",
    )
    assert manager.update_task_env("alpha", {"GOOD": "bad\x00value"}, {"B": "2"}) == (
        False,
        "environment variable 'GOOD' contains a null byte",
    )

    assert manager.rename_task("alpha", "") == (False, "Task name cannot be empty")
    assert manager.rename_task("missing", "new") == (False, "Task not found")
    with manager._lock:
        manager._tasks_by_name["alpha"]["status"] = "queued"
    assert manager.rename_task("alpha", "alpha-new") == (False, "Running or queued tasks cannot be renamed")
    with manager._lock:
        manager._tasks_by_name["alpha"]["status"] = "pending"
    assert manager.rename_task("alpha", "alpha") == (True, "alpha")
    assert "invalid" in manager.rename_task("alpha", "bad/name")[1]
    assert "already exists" in manager.rename_task("alpha", "beta")[1]

    with patch("pyruns.core.task_manager.os.rename", lambda old, new: (_ for _ in ()).throw(OSError("rename failed"))):
        assert manager.rename_task("alpha", "gamma") == (False, "rename failed")

    with patch("pyruns.core.task_manager.update_task_metadata", side_effect=RuntimeError("write failed")):
        ok, message = manager.rename_task("alpha", "gamma")
    assert ok is False
    assert "write failed" in message
    assert Path(alpha["dir"]).exists()
    assert Path(beta["dir"]).exists()


def test_task_manager_reorder_rolls_back_partial_writes(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    alpha = generator.create_task("alpha", {"value": 1})
    beta = generator.create_task("beta", {"value": 2})

    manager = _make_task_manager(tasks_dir)

    write_count = 0

    def fail_second_write(task_dir, updater):
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise OSError("shared filesystem write failed")
        return update_task_info(task_dir, updater)

    monkeypatch.setattr("pyruns.core.task_manager.update_task_metadata", fail_second_write)

    with pytest.raises(OSError, match="shared filesystem write failed"):
        manager.reorder_tasks([
            {"name": "beta", "pinned": True},
            {"name": "alpha", "pinned": False},
        ])

    assert write_count == 3
    for task in (alpha, beta):
        persisted = load_task_info(task["dir"], raise_error=True)
        assert "task_order" not in persisted
        assert persisted["pinned"] is False
        current = manager.get_task(task["name"])
        assert current["task_order"] is None
        assert current["pinned"] is False


def test_task_manager_reorder_serializes_concurrent_batches(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    for name in ("alpha", "beta", "gamma"):
        generator.create_task(name, {"value": name})

    manager_a = TaskManager(
        tasks_dir=str(tasks_dir),
        lazy_scan=False,
        owns_task_lifecycle=False,
    )
    manager_b = TaskManager(
        tasks_dir=str(tasks_dir),
        lazy_scan=False,
        owns_task_lifecycle=False,
    )

    real_update_task_info = update_task_info
    first_updates = threading.Barrier(2)
    calls: list[int] = []
    seen_threads: set[int] = set()
    calls_lock = threading.Lock()

    def coordinated_update(task_dir, updater):
        thread_id = threading.get_ident()
        with calls_lock:
            calls.append(thread_id)
            first_for_thread = thread_id not in seen_threads
            seen_threads.add(thread_id)
        if first_for_thread:
            try:
                first_updates.wait(timeout=0.2)
            except threading.BrokenBarrierError:
                pass
        return real_update_task_info(task_dir, updater)

    monkeypatch.setattr("pyruns.core.task_manager.update_task_metadata", coordinated_update)
    forward = [{"name": name} for name in ("alpha", "beta", "gamma")]
    reverse = [{"name": name} for name in ("gamma", "beta", "alpha")]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            pool.submit(manager_a.reorder_tasks, forward),
            pool.submit(manager_b.reorder_tasks, reverse),
        ]
        outcomes = [result.result(timeout=5) for result in results]

    assert all(ok for ok, _ in outcomes)
    assert len(calls) == 6
    assert len(set(calls)) == 2
    assert sum(left != right for left, right in zip(calls, calls[1:])) == 1

    persisted_order = tuple(
        load_task_info(str(tasks_dir / name), raise_error=True)["task_order"]
        for name in ("alpha", "beta", "gamma")
    )
    assert persisted_order in {(0, 1, 2), (2, 1, 0)}


def test_task_manager_delete_active_task_preserves_folder_when_trash_move_fails(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "runner"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "runner",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [12345],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})
    trash_conflict = tasks_dir / TRASH_DIR / "runner"
    trash_conflict.mkdir(parents=True)

    killed = []
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda pid: True)
    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda pid, expected_create_time=None: killed.append(pid) or True,
    )
    monkeypatch.setattr("pyruns.core.task_manager.get_now_str", lambda: "2026-03-20_00-00-02")
    monkeypatch.setattr("pyruns.core.task_manager.os.rename", lambda src, dst: (_ for _ in ()).throw(OSError("move failed")))
    monkeypatch.setattr("pyruns.core.task_manager.time.sleep", lambda delay: None)

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "runner", task_dir)

    def settle(*_args, **_kwargs):
        update_task_info(
            str(task_dir),
            lambda info: info.update({"status": "cancelled"}),
        )
        with manager._lock:
            manager._clear_running_locked("runner")
        return load_task_info(str(task_dir))

    monkeypatch.setattr(manager, "_wait_for_task_settle", settle)

    manager.delete_tasks(["missing"])
    assert manager.get_task("runner") is not None

    deleted = manager.delete_tasks(["runner", "runner"])

    assert killed == [12345]
    assert deleted == []
    assert task_dir.exists()
    assert manager.get_task("runner")["status"] == "cancelled"


def test_task_manager_keeps_live_foreign_runner_running(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "remote"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "remote",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [12345],
            "runner_id": "other-host:123:abcdef",
            "runner_host": "other-host",
            "lease_until": time.time() + 60,
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda pid: False)

    manager = _make_task_manager(tasks_dir)

    assert manager.get_task("remote")["status"] == "running"


def test_task_manager_refresh_keeps_expired_remote_runner_when_metadata_changes(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "remote"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "remote",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [12345],
            "runner_id": "other-host:123:abcdef",
            "runner_host": "other-host",
            "lease_until": time.time() + 60,
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda pid: False)

    manager = _make_task_manager(tasks_dir)

    task = manager.get_task("remote")
    assert task["status"] == "running"
    original_mtime_ns = task["_mtime_ns"]
    assert manager.refresh_from_disk() is False

    update_task_info(str(task_dir), lambda info: info.update({"lease_until": time.time() - 60}))
    expired_mtime_ns = (task_dir / TASK_INFO_FILENAME).stat().st_mtime_ns

    assert manager.refresh_from_disk() is True
    refreshed = manager.get_task("remote")
    assert refreshed["status"] == "running"
    assert refreshed["_mtime_ns"] == expired_mtime_ns
    assert refreshed["_mtime_ns"] >= original_mtime_ns
    assert load_task_info(str(task_dir))["status"] == "running"

    manager._cleanup_on_shutdown()

    assert load_task_info(str(task_dir))["status"] == "running"


def test_task_manager_does_not_submit_when_foreign_runner_owns_lease(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    task = generator.create_task("alpha", {"value": 1})
    save_task_info(
        task["dir"],
        {
            "name": "alpha",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 2,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [4321],
            "runner_id": "other-host:4321:abcdef",
            "runner_host": "other-host",
            "lease_until": time.time() + 60,
        },
    )

    manager = _make_task_manager(tasks_dir)

    submitted = []

    class CapturingExecutor:
        def submit(self, *args, **kwargs):
            submitted.append((args, kwargs))

    manager._executor = CapturingExecutor()
    monkeypatch.setattr(manager, "_ensure_executor", lambda: None)
    with manager._lock:
        target = manager._tasks_by_name["alpha"]
        target["status"] = "queued"
        manager._mark_running_locked("alpha", counts_for_batch=True)

    manager._submit_task(target, 3, independent=False)

    assert submitted == []
    assert manager.get_task("alpha")["status"] == "running"
    assert "alpha" not in manager._running_ids
    assert "alpha" not in manager._batch_running_ids


def test_task_manager_start_batch_sync_conflict_keeps_foreign_runner_without_submit(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})

    manager = _make_task_manager(tasks_dir)

    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "running",
            "run_index": 4,
            "runner_id": "other-host:4321:abcdef",
            "runner_host": "other-host",
            "lease_until": time.time() + 60,
            "pids": [4321],
        }),
    )
    submitted = []
    monkeypatch.setattr(
        manager,
        "_submit_task",
        lambda target, run_index, *, independent: submitted.append(target["name"]),
    )

    claimed = manager.start_batch_tasks(["alpha"], max_workers=1)

    refreshed = manager.get_task("alpha")
    assert claimed == []
    assert submitted == []
    assert refreshed["status"] == "running"
    assert refreshed["run_index"] == 4
    assert refreshed["runner_id"] == "other-host:4321:abcdef"
    assert "alpha" not in manager._running_ids


def test_task_manager_expected_run_rejects_completed_race_without_run_two(
    tmp_path,
    monkeypatch,
):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("race", {"value": 1})

    manager = _make_task_manager(tasks_dir)

    def complete_first_run(info):
        slot = ensure_run_slot(info, 1)
        info["status"] = "completed"
        info["start_times"][slot] = "2026-08-10_10-00-00"
        info["finish_times"][slot] = "2026-08-10_10-00-01"
        info["run_statuses"][slot] = "completed"
        info["exit_codes"][slot] = 0

    update_task_info(task["dir"], complete_first_run)
    submitted: list[tuple[str, int]] = []
    monkeypatch.setattr(
        manager,
        "_submit_task",
        lambda target, run_index, *, independent: submitted.append(
            (str(target["name"]), run_index)
        ),
    )

    for _ in range(2):
        assert manager.start_batch_tasks(
            ["race"],
            max_workers=1,
            expected_run_indices={"race": 1},
        ) == []

    info = load_task_info(task["dir"])
    assert submitted == []
    assert info["status"] == "completed"
    assert info["run_index"] == 1
    assert info["run_statuses"] == ["completed"]
    assert manager.get_task("race")["run_index"] == 1
    assert "race" not in manager._running_ids


def test_task_manager_gpu_queue_sync_conflict_skips_wait_log_and_clears_transient_state(tmp_path, monkeypatch):
    settings_root = tmp_path
    tasks_dir = settings_root / "tasks"
    tasks_dir.mkdir()
    (settings_root / "_pyruns_settings.yaml").write_text(
        "gpu_scheduler_enabled: true\ngpu_scheduler_stable_seconds: 1\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-race", {"value": 1})

    manager = _make_task_manager(tasks_dir)

    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "running",
            "run_index": 3,
            "runner_id": "other-host:123:abcdef",
            "runner_host": "other-host",
            "lease_until": time.time() + 60,
            "pids": [123],
        }),
    )
    logged = []
    monkeypatch.setattr(manager, "_append_gpu_wait_started", lambda *args, **kwargs: logged.append(args))

    manager.start_batch_tasks(["gpu-race"], max_workers=1)

    refreshed = manager.get_task("gpu-race")
    assert logged == []
    assert refreshed["status"] == "running"
    assert refreshed["run_index"] == 3
    assert refreshed["runner_id"] == "other-host:123:abcdef"
    assert not (Path(task["dir"]) / RUN_LOGS_DIR / "queue.log").exists()
    with manager._lock:
        current = manager._tasks_by_name["gpu-race"]
        assert "_gpu_wait_started_at" not in current
        assert "_queued_independent" not in current
    assert "gpu-race" not in manager._running_ids


def test_task_manager_rerun_returns_false_when_queue_sync_conflicts_with_foreign_runner(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    update_task_info(task["dir"], lambda info: info.update({"status": "completed", "run_index": 1}))

    manager = _make_task_manager(tasks_dir)

    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "running",
            "run_index": 2,
            "runner_id": "other-host:987:abcdef",
            "runner_host": "other-host",
            "lease_until": time.time() + 60,
            "pids": [987],
        }),
    )

    assert manager.rerun_task("alpha") is False
    refreshed = manager.get_task("alpha")
    assert refreshed["status"] == "running"
    assert refreshed["run_index"] == 2
    assert refreshed["runner_id"] == "other-host:987:abcdef"


@pytest.mark.parametrize("entrypoint", ["batch", "start", "rerun"])
def test_task_manager_rejects_run_history_overflow_before_changing_state(
    tmp_path,
    entrypoint,
):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("history-full", {"value": 1})
    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "completed",
            "run_index": MAX_RUN_HISTORY_SLOTS,
        }),
    )
    before = load_task_info(task["dir"], raise_error=True)

    manager = _make_task_manager(tasks_dir)

    with pytest.raises(ValueError, match="reached the run history limit"):
        if entrypoint == "batch":
            manager.start_batch_tasks([task["name"]])
        elif entrypoint == "start":
            manager.start_task_now(task["name"])
        else:
            manager.rerun_task(task["name"])

    assert load_task_info(task["dir"], raise_error=True) == before
    assert manager.get_task(task["name"])["status"] == "completed"


def test_task_manager_rejects_starts_after_shutdown(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("stopped", {"value": 1})
    update_task_info(task["dir"], lambda info: info.update({"status": "completed"}))

    manager = _make_task_manager(tasks_dir)

    manager.shutdown()
    monkeypatch.setattr(
        task_manager_module,
        "ThreadPoolExecutor",
        lambda *args, **kwargs: pytest.fail("executor must not be created after shutdown"),
    )

    assert manager.start_batch_tasks([task["name"]]) == []
    assert manager.start_task_now(task["name"]) is False
    assert manager.rerun_task(task["name"]) is False
    with pytest.raises(RuntimeError, match="shutting down"):
        manager._ensure_executor()
    assert manager._executor is None
    assert manager._independent_executor is None


def test_task_manager_shutdown_waits_for_start_lifecycle_section(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    manager = _make_task_manager(tasks_dir)

    start_entered = threading.Event()
    release_start = threading.Event()
    shutdown_entered = threading.Event()
    shutdown_done = threading.Event()

    def blocked_start(_task_id):
        start_entered.set()
        assert release_start.wait(timeout=2)
        return True

    monkeypatch.setattr(manager, "_start_task_now", blocked_start)
    start_thread = threading.Thread(target=manager.start_task_now, args=("alpha",))
    start_thread.start()
    assert start_entered.wait(timeout=1)

    def run_shutdown():
        shutdown_entered.set()
        manager.shutdown()
        shutdown_done.set()

    shutdown_thread = threading.Thread(target=run_shutdown)
    shutdown_thread.start()
    assert shutdown_entered.wait(timeout=1)
    assert manager._shutdown_event.is_set() is False
    assert shutdown_done.is_set() is False

    release_start.set()
    start_thread.join(timeout=2)
    shutdown_thread.join(timeout=2)

    assert start_thread.is_alive() is False
    assert shutdown_thread.is_alive() is False
    assert shutdown_done.is_set() is True
    assert manager._shutdown_event.is_set() is True


def test_task_manager_does_not_claim_or_queue_creation_rollback_tombstone(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("rolled-back", {"value": 1})
    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "cancelled",
            "_creation_rollback": {"token": "creator-token"},
        }),
    )

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        current = manager._tasks_by_name[task["name"]]
        current["status"] = "queued"
    assert manager._claim_task_for_run(current, 1, counts_for_batch=True) is None

    with manager._lock:
        manager._tasks_by_name[task["name"]]["status"] = "pending"
    assert manager._sync_status_to_disk(
        task["name"],
        "queued",
        run_index=1,
        expected_statuses={"pending"},
    ) is False

    persisted = load_task_info(task["dir"], raise_error=True)
    assert persisted["status"] == "cancelled"
    assert persisted["_creation_rollback"] == {"token": "creator-token"}


def test_task_manager_delete_marker_blocks_concurrent_start(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    real_rename = os.rename
    start_results = []

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        deleting = TaskManager(
            tasks_dir=str(tasks_dir),
            lazy_scan=False,
            owns_task_lifecycle=False,
        )
        contender = TaskManager(
            tasks_dir=str(tasks_dir),
            lazy_scan=False,
            owns_task_lifecycle=False,
        )

    def rename_after_start_attempt(source, destination):
        if os.path.normcase(os.path.abspath(source)) == os.path.normcase(task["dir"]):
            start_results.append(contender.start_task_now("alpha"))
            marked = load_task_info(task["dir"], raise_error=True)
            assert marked["_namespace_operation"]["kind"] == "delete"
        return real_rename(source, destination)

    monkeypatch.setattr("pyruns.core.task_manager.os.rename", rename_after_start_attempt)
    try:
        assert deleting.delete_tasks(["alpha"]) == ["alpha"]
        assert start_results == [False]
        trashed = load_task_info(str(tasks_dir / TRASH_DIR / "alpha"), raise_error=True)
        assert "_namespace_operation" not in trashed
    finally:
        deleting.shutdown()
        contender.shutdown()


def test_task_manager_rename_marker_blocks_concurrent_start(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    real_rename = os.rename
    start_results = []

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        renaming = TaskManager(
            tasks_dir=str(tasks_dir),
            lazy_scan=False,
            owns_task_lifecycle=False,
        )
        contender = TaskManager(
            tasks_dir=str(tasks_dir),
            lazy_scan=False,
            owns_task_lifecycle=False,
        )

    def rename_after_start_attempt(source, destination):
        if os.path.normcase(os.path.abspath(source)) == os.path.normcase(task["dir"]):
            start_results.append(contender.start_task_now("alpha"))
            marked = load_task_info(task["dir"], raise_error=True)
            assert marked["_namespace_operation"]["kind"] == "rename"
        return real_rename(source, destination)

    monkeypatch.setattr("pyruns.core.task_manager.os.rename", rename_after_start_attempt)
    try:
        assert renaming.rename_task("alpha", "beta") == (True, "beta")
        assert start_results == [False]
        renamed = load_task_info(str(tasks_dir / "beta"), raise_error=True)
        assert renamed["name"] == "beta"
        assert "_namespace_operation" not in renamed
    finally:
        renaming.shutdown()
        contender.shutdown()


def test_task_manager_delete_rechecks_status_before_moving(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(
            tasks_dir=str(tasks_dir),
            lazy_scan=False,
            owns_task_lifecycle=False,
        )

    real_begin = manager._begin_namespace_operation

    def begin_after_concurrent_rerun(*args, **kwargs):
        update_task_info(
            task["dir"],
            lambda info: info.update({"status": "queued", "run_index": 1}),
        )
        return real_begin(*args, **kwargs)

    monkeypatch.setattr(manager, "_begin_namespace_operation", begin_after_concurrent_rerun)
    try:
        assert manager.delete_tasks(["alpha"]) == []
        assert Path(task["dir"]).is_dir()
        assert load_task_info(task["dir"], raise_error=True)["status"] == "queued"
        assert not (tasks_dir / TRASH_DIR / "alpha").exists()
    finally:
        manager.shutdown()


def test_task_manager_delete_binds_stop_to_original_run(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(
            tasks_dir=str(tasks_dir),
            lazy_scan=False,
            owns_task_lifecycle=False,
        )
    update_task_info(
        task["dir"],
        lambda info: info.update(
            {
                "status": "running",
                "run_index": 1,
                "pids": [12345],
                "pid_create_times": [1000.0],
                "runner_id": manager.runner_id,
                "runner_host": manager.runner_host,
                "lease_heartbeat": time.time(),
                "lease_until": time.time() + 60,
            }
        ),
    )
    manager.scan_disk()
    real_persist = manager._persist_pending_stop_summary

    def persist_after_concurrent_rerun(*args, **kwargs):
        update_task_info(
            task["dir"],
            lambda info: info.update(
                {
                    "status": "running",
                    "run_index": 2,
                    "runner_id": manager.runner_id,
                    "runner_host": manager.runner_host,
                    "lease_heartbeat": time.time(),
                    "lease_until": time.time() + 60,
                }
            ),
        )
        return real_persist(*args, **kwargs)

    killed = []
    monkeypatch.setattr(manager, "_persist_pending_stop_summary", persist_after_concurrent_rerun)
    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda pid, expected_create_time=None: killed.append(pid) or True,
    )
    try:
        assert manager.delete_tasks(["alpha"]) == []
        persisted = load_task_info(task["dir"], raise_error=True)
        assert persisted["status"] == "running"
        assert persisted["run_index"] == 2
        assert killed == []
        assert not (tasks_dir / TRASH_DIR / "alpha").exists()
    finally:
        manager.shutdown()


def test_task_manager_discards_expired_namespace_marker_when_starting(tmp_path):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    update_task_info(
        task["dir"],
        lambda info: info.update(
            {
                "_namespace_operation": {
                    "kind": "delete",
                    "token": "expired",
                    "host": socket.gethostname().lower(),
                    "pid": os.getpid(),
                    "pid_create_time": task_manager_module.get_process_create_time(os.getpid()),
                    "expires_at": time.time() - 1,
                }
            }
        ),
    )

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(
            tasks_dir=str(tasks_dir),
            lazy_scan=False,
            owns_task_lifecycle=False,
        )
    try:
        assert manager._sync_status_to_disk(
            "alpha",
            "queued",
            run_index=1,
            expected_statuses={"pending"},
        ) is True
        persisted = load_task_info(task["dir"], raise_error=True)
        assert persisted["status"] == "queued"
        assert "_namespace_operation" not in persisted
    finally:
        manager.shutdown()


def test_task_manager_internal_executor_and_worker_error_paths(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    task = generator.create_task("alpha", {"value": 1})

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        target = manager._tasks_by_name["alpha"]
        target["status"] = "queued"
        target["run_index"] = 2
        target["start_times"] = ["2026-01-01_00-00-00", "2026-01-01_00-00-02"]
        target["finish_times"] = ["2026-01-01_00-00-01", "2026-01-01_00-00-03"]
        manager._recompute_processing_flag_locked()
    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "queued",
            "run_index": 2,
            "start_times": ["2026-01-01_00-00-00", "2026-01-01_00-00-02"],
            "finish_times": ["2026-01-01_00-00-01", "2026-01-01_00-00-03"],
        }),
    )

    picked, run_index = manager._pick_queued_task()
    assert picked["name"] == "alpha"
    assert run_index == 3
    assert manager.get_task("alpha")["status"] == "running"

    class FailingExecutor:
        def submit(self, *args, **kwargs):
            raise RuntimeError("submit failed")

    manager._executor = FailingExecutor()
    monkeypatch.setattr(manager, "_ensure_executor", lambda: None)
    manager._submit_task(picked, 3, independent=False)
    assert picked["status"] == "failed"

    manager.runner_id = "local-runner"
    update_task_info(
        task["dir"],
        lambda info: info.update(
            {
                "status": "running",
                "run_index": 4,
                "runner_id": manager.runner_id,
                "runner_host": manager.runner_host,
            }
        ),
    )
    with manager._lock:
        manager._apply_info_to_task(picked, load_task_info(task["dir"]))
        picked["status"] = "running"
        manager._mark_running_locked("alpha", counts_for_batch=True)

    future = Future()
    future.set_exception(RuntimeError("worker failed"))
    manager._on_task_done(
        future,
        "alpha",
        expected_runner_id=manager.runner_id,
        expected_run_index=4,
    )

    assert manager.get_task("alpha")["status"] == "failed"
    assert "alpha" not in manager._batch_running_ids


@pytest.mark.parametrize("independent", [False, True])
@pytest.mark.parametrize("final_write_fails", [False, True])
def test_task_manager_failed_submission_finalizes_or_retries_metadata(
    tmp_path, monkeypatch, independent, final_write_fails,
):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    assert manager._sync_status_to_disk("alpha", "running", run_index=1, counts_for_batch=not independent)

    class RejectingExecutor:
        def submit(self, *args, **kwargs):
            raise RuntimeError("executor rejected submission")

    if independent:
        manager._independent_executor = RejectingExecutor()
    else:
        manager._executor = RejectingExecutor()
        monkeypatch.setattr(manager, "_ensure_executor", lambda: None)

    def fail_final_write(*args, **kwargs):
        raise PermissionError("read-only task metadata")

    if final_write_fails:
        monkeypatch.setattr(manager, "_mark_failed_on_disk", fail_final_write)
    manager._submit_task(manager._tasks_by_name["alpha"], 1, independent=independent)

    assert "alpha" not in manager._running_ids
    assert "alpha" not in manager._batch_running_ids
    assert manager.is_processing is False
    current = manager.get_task("alpha")
    assert current["status"] == "failed"
    if final_write_fails:
        assert "read-only task metadata" in current["_load_error"]
    else:
        assert current["_load_error"] == ""
        assert load_task_info(task["dir"])["status"] == "failed"
        error_text = (Path(task["dir"]) / RUN_LOGS_DIR / ERROR_LOG_FILENAME).read_text(encoding="utf-8")
        assert "reason=submission_error" in error_text
        assert f"independent={independent}" in error_text
        assert "executor rejected submission" in error_text


@pytest.mark.parametrize("independent", [False, True])
@pytest.mark.parametrize("final_write_fails", [False, True])
def test_task_manager_rejected_submission_cannot_execute_later(
    tmp_path, monkeypatch, independent, final_write_fails,
):
    tasks_dir = tmp_path / "tasks"
    TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    assert manager._sync_status_to_disk("alpha", "running", run_index=1, counts_for_batch=not independent)
    launched = []
    monkeypatch.setattr(task_manager_module, "run_task_worker", lambda *args: launched.append(args[1]))

    def fail_final_write(*args, **kwargs):
        raise PermissionError("read-only task metadata")

    if final_write_fails:
        monkeypatch.setattr(manager, "_mark_failed_on_disk", fail_final_write)
    with ThreadPoolExecutor(max_workers=1) as pool:
        if independent:
            manager._independent_executor = pool
        else:
            manager._executor = pool
            monkeypatch.setattr(manager, "_ensure_executor", lambda: None)
        # A real pool enqueues the work before trying to start its first thread.
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("cannot start thread")):
            manager._submit_task(manager._tasks_by_name["alpha"], 1, independent=independent)

        assert manager.get_task("alpha")["status"] == "failed"
        assert "alpha" not in manager._running_ids
        # Starting another worker must drain the rejected item without launching it.
        pool.submit(lambda: None).result(timeout=5)

    assert launched == []


@pytest.mark.parametrize("disk_status", ["queued", "running"])
@pytest.mark.parametrize("independent", [False, True])
def test_task_manager_claim_write_error_does_not_hold_a_worker_slot(tmp_path, monkeypatch, disk_status, independent):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    assert manager._sync_status_to_disk("alpha", disk_status, run_index=1, counts_for_batch=not independent)
    if disk_status == "queued":
        with manager._lock:
            manager._tasks_by_name["alpha"]["_queued_independent"] = independent
        target, run_index = manager._pick_queued_task()
        target.pop("_queued_independent", None)
    else:
        target, run_index = manager._tasks_by_name["alpha"], 1

    def fail_write(*args, **kwargs):
        raise PermissionError("read-only task metadata")

    original_update = task_manager_module.update_task_metadata
    monkeypatch.setattr(task_manager_module, "update_task_metadata", fail_write)
    try:
        manager._submit_task(target, run_index, independent=independent)
    except PermissionError:
        pass

    assert "alpha" not in manager._running_ids
    assert "alpha" not in manager._batch_running_ids
    assert load_task_info(task["dir"])["status"] == disk_status
    assert manager.get_task("alpha")["status"] != "running"
    monkeypatch.setattr(task_manager_module, "update_task_metadata", original_update)
    if disk_status == "queued":
        picked, next_run = manager._pick_queued_task(independent_only=independent)
        assert picked is not None
        assert next_run == 1
        assert ("alpha" in manager._batch_running_ids) is (not independent)
        assert manager._claim_task_for_run(picked, next_run, counts_for_batch=not independent)
    else:
        manager.owns_task_lifecycle = True
        manager.refresh_from_disk()
        assert load_task_info(task["dir"])["status"] == "failed"
        assert manager.get_task("alpha")["_load_error"] == ""


def test_task_manager_failed_queue_claim_allows_next_task_to_run(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    generator = TaskGenerator(root_dir=str(tasks_dir))
    alpha = generator.create_task("alpha", {"value": 1})
    generator.create_task("beta", {"value": 2})
    generator.create_task("gamma", {"value": 3})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    assert manager._sync_status_to_disk("alpha", "queued", run_index=1)
    assert manager._sync_status_to_disk("beta", "queued", run_index=1)
    target, run_index = manager._pick_queued_task()
    assert target["name"] == "alpha"
    original_update = task_manager_module.update_task_metadata

    def fail_alpha(task_dir, *args, **kwargs):
        if Path(task_dir) == Path(alpha["dir"]):
            raise PermissionError("alpha metadata is read-only")
        return original_update(task_dir, *args, **kwargs)

    monkeypatch.setattr(task_manager_module, "update_task_metadata", fail_alpha)
    with pytest.raises(PermissionError, match="read-only"):
        manager._submit_task(target, run_index, independent=False)

    next_task, next_run = manager._pick_queued_task()
    assert next_task["name"] == "beta"
    assert manager._claim_task_for_run(next_task, next_run, counts_for_batch=True)

    assert manager._sync_status_to_disk("gamma", "queued", run_index=1)
    assert manager._sync_status_to_disk("beta", "completed", run_index=1)
    monkeypatch.setattr(task_manager_module, "update_task_metadata", original_update)
    manager._refresh_queued_runner_leases()

    recovered_task, recovered_run = manager._pick_queued_task()
    assert recovered_task["name"] == "alpha"
    assert recovered_run == 1


def test_task_manager_queue_heartbeat_continues_after_one_metadata_write_fails(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    generator = TaskGenerator(root_dir=str(tasks_dir))
    beta = generator.create_task("beta", {"value": 2})
    alpha = generator.create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    assert manager._sync_status_to_disk("alpha", "queued", run_index=1)
    assert manager._sync_status_to_disk("beta", "queued", run_index=1)
    update_task_info(beta["dir"], lambda info: info.update({"lease_heartbeat": 1.0}))
    original_update = task_manager_module.update_task_metadata

    def fail_alpha(task_dir, *args, **kwargs):
        if Path(task_dir) == Path(alpha["dir"]):
            raise PermissionError("alpha metadata is read-only")
        return original_update(task_dir, *args, **kwargs)

    monkeypatch.setattr(task_manager_module, "update_task_metadata", fail_alpha)
    manager._refresh_queued_runner_leases()

    assert load_task_info(beta["dir"])["lease_heartbeat"] > 1.0


@pytest.mark.parametrize("wall_clock_shift", [-3600.0, 3600.0])
def test_task_manager_queue_heartbeat_ignores_wall_clock_adjustments(tmp_path, monkeypatch, wall_clock_shift):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    assert manager._sync_status_to_disk("alpha", "queued", run_index=1)
    wall_now = [100_000.0]
    elapsed_now = [100.0]
    clock = MagicMock(wraps=time)
    clock.time.side_effect = lambda: wall_now[0]
    clock.monotonic.side_effect = lambda: elapsed_now[0]
    monkeypatch.setattr(task_manager_module, "time", clock)

    with patch.object(task_manager_module, "update_task_metadata", wraps=update_task_info) as write:
        manager._refresh_queued_runner_leases()
        assert write.call_count == 1

        wall_now[0] += wall_clock_shift
        elapsed_now[0] += 0.1
        manager._refresh_queued_runner_leases()
        assert write.call_count == 1

        elapsed_now[0] += manager.lease_seconds
        manager._refresh_queued_runner_leases()
        assert write.call_count == 2

    assert load_task_info(task["dir"])["lease_heartbeat"] == wall_now[0]


@pytest.mark.parametrize("claim_persisted", [False, True])
def test_task_manager_queue_heartbeat_does_not_requeue_selected_task(tmp_path, monkeypatch, claim_persisted):
    tasks_dir = tmp_path / "tasks"
    TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    assert manager._sync_status_to_disk("alpha", "queued", run_index=1)
    original_update = task_manager_module.update_task_metadata
    selected = False

    def renew_then_select(task_dir, *args, **kwargs):
        nonlocal selected
        updated = original_update(task_dir, *args, **kwargs)
        if not selected:
            selected = True
            picked, run_index = manager._pick_queued_task()
            assert picked["name"] == "alpha"
            if claim_persisted:
                assert manager._claim_task_for_run(picked, run_index, counts_for_batch=True)
        return updated

    monkeypatch.setattr(task_manager_module, "update_task_metadata", renew_then_select)
    manager._refresh_queued_runner_leases()

    assert manager.get_task("alpha")["status"] == "running"
    assert manager.get_task("alpha")["run_index"] == 1
    assert "alpha" in manager._batch_running_ids
    assert manager._pick_queued_task()[0] is None


def test_task_manager_queue_heartbeat_keeps_new_gpu_wait_generation(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    config = GpuSchedulerConfig(enabled=True, max_wait_seconds=600)
    old_wait = manager._new_gpu_wait_state(1, config, started_at=100)
    new_wait = manager._new_gpu_wait_state(1, config, started_at=200)
    assert manager._sync_status_to_disk("alpha", "queued", run_index=1, gpu_wait=old_wait)
    original_update = task_manager_module.update_task_metadata

    def requeue_then_renew(task_dir, *args, **kwargs):
        updated = original_update(task_dir, lambda info: info.update({
            "queued_at": new_wait["started_at"], "gpu_wait": new_wait,
        }))
        with manager._lock:
            manager._apply_info_to_task(manager._tasks_by_name["alpha"], updated)
        return original_update(task_dir, *args, **kwargs)

    monkeypatch.setattr(task_manager_module, "update_task_metadata", requeue_then_renew)
    manager._refresh_queued_runner_leases()

    assert load_task_info(task["dir"])["gpu_wait"]["started_at"] == 200
    assert manager.get_task("alpha")["gpu_wait"]["deadline_at"] == 800


def test_task_manager_queue_heartbeat_renews_legacy_gpu_wait_without_queued_at(tmp_path):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    config = GpuSchedulerConfig(enabled=True, max_wait_seconds=600)
    wait = TaskManager._new_gpu_wait_state(1, config, started_at=100)
    update_task_info(task["dir"], lambda info: info.update({
        "status": "queued", "gpu_wait": wait, "lease_heartbeat": 1.0,
    }))
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    with manager._lock:
        manager._ensure_gpu_wait_state(manager._tasks_by_name["alpha"], 1, config, now=150)

    manager._refresh_queued_runner_leases()

    renewed = load_task_info(task["dir"])
    assert renewed["lease_heartbeat"] > 1.0
    assert renewed["gpu_wait"]["started_at"] == 100


@pytest.mark.parametrize("operation", ["pin", "notes", "env", "reorder"])
@pytest.mark.parametrize("claim_persisted", [False, True])
def test_task_manager_metadata_edit_keeps_selected_task_running(tmp_path, monkeypatch, operation, claim_persisted):
    tasks_dir = tmp_path / "tasks"
    TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    assert manager._sync_status_to_disk("alpha", "queued", run_index=1)
    original_update = task_manager_module.update_task_metadata
    selected = False

    def save_then_select(task_dir, *args, **kwargs):
        nonlocal selected
        updated = original_update(task_dir, *args, **kwargs)
        if not selected:
            selected = True
            picked, run_index = manager._pick_queued_task()
            assert picked["name"] == "alpha"
            if claim_persisted:
                assert manager._claim_task_for_run(picked, run_index, counts_for_batch=True)
        return updated

    monkeypatch.setattr(task_manager_module, "update_task_metadata", save_then_select)
    edits = {
        "pin": lambda: manager.set_task_pinned("alpha", True),
        "notes": lambda: manager.update_task_notes("alpha", "updated note", ""),
        "env": lambda: manager.update_task_env("alpha", {"OMP_NUM_THREADS": "2"}, {}),
        "reorder": lambda: manager.reorder_tasks([{"name": "alpha", "pinned": True}]),
    }
    assert edits[operation]()[0] is True

    current = manager.get_task("alpha")
    assert current["status"] == "running"
    assert current["run_index"] == 1
    assert "alpha" in manager._batch_running_ids
    assert manager._pick_queued_task()[0] is None
    if operation in {"pin", "reorder"}:
        assert current["pinned"] is True
    elif operation == "notes":
        assert current["notes"] == "updated note"
    else:
        assert current["env"] == {"OMP_NUM_THREADS": "2"}


@pytest.mark.parametrize("concurrent_edit", ["notes", "env"])
def test_task_manager_metadata_edit_preserves_later_edits(tmp_path, monkeypatch, concurrent_edit):
    tasks_dir = tmp_path / "tasks"
    TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    original_update = task_manager_module.update_task_metadata
    changed = False

    def save_then_edit(task_dir, *args, **kwargs):
        nonlocal changed
        updated = original_update(task_dir, *args, **kwargs)
        if not changed:
            changed = True
            if concurrent_edit == "notes":
                assert manager.update_task_notes("alpha", "second note", "first note")[0] is True
            else:
                assert manager.update_task_env("alpha", {"OMP_NUM_THREADS": "2"}, {})[0] is True
        return updated

    monkeypatch.setattr(task_manager_module, "update_task_metadata", save_then_edit)
    assert manager.update_task_notes("alpha", "first note", "")[0] is True

    current = manager.get_task("alpha")
    if concurrent_edit == "notes":
        assert current["notes"] == "second note"
    else:
        assert current["notes"] == "first note"
        assert current["env"] == {"OMP_NUM_THREADS": "2"}


@pytest.mark.parametrize("new_status", ["queued", "running"])
def test_task_manager_stale_claim_does_not_rewind_a_new_run(tmp_path, new_status):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    assert manager._sync_status_to_disk("alpha", "running", run_index=1)
    update_task_info(task["dir"], lambda info: info.update({"status": "completed", "run_statuses": ["completed"]}))
    assert manager._sync_status_to_disk("alpha", new_status, run_index=2)

    claimed = manager._claim_task_for_run(manager._tasks_by_name["alpha"], 1, counts_for_batch=True)

    assert claimed is None
    persisted = load_task_info(task["dir"])
    assert persisted["status"] == new_status
    assert persisted["run_index"] == (1 if new_status == "queued" else 2)
    assert persisted["run_statuses"][0] == "completed"
    manager._submit_task(manager._tasks_by_name["alpha"], 1, independent=False)
    if new_status == "running":
        assert "alpha" in manager._running_ids
        assert manager.get_task("alpha")["run_index"] == 2


def test_task_manager_gpu_claim_rejection_preserves_new_run_resources(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    assert manager._sync_status_to_disk("alpha", "queued", run_index=1)
    config = GpuSchedulerConfig(enabled=True, min_free_memory_gb=8, stable_seconds=1)
    now = [100.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider([GpuDevice(0, "A800", "GPU-0", 2048, 40960, 1)]),
        clock=lambda: now[0],
    )
    manager.gpu_scheduler.snapshot(config)
    now[0] += 1

    def claim_after_restart(*args, **kwargs):
        updated = update_task_info(task["dir"], lambda info: info.update({
            "status": "running", "run_index": 2,
            "_gpu_assignment": {"run_index": 2, "gpu_ids": [0]},
        }))
        with manager._lock:
            manager._apply_info_to_task(manager._tasks_by_name["alpha"], updated)
            manager._mark_running_locked("alpha", counts_for_batch=True)
        return None

    monkeypatch.setattr(manager, "_claim_task_for_run", claim_after_restart)
    picked, _ = manager._pick_queued_gpu_task(config, independent_only=False)

    assert picked is None
    current = manager.get_task("alpha")
    assert current["status"] == "running"
    assert current["run_index"] == 2
    assert current["_gpu_assignment"]["run_index"] == 2
    assert "alpha" in manager._batch_running_ids
    assert manager.gpu_scheduler._reservations == {"alpha": [0]}


def test_task_manager_old_worker_callback_does_not_clear_new_run(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("restarted", {"value": 1})
    manager = _make_task_manager(tasks_dir)

    def start_new_run(info):
        info.update(
            {
                "status": "running",
                "run_index": 2,
                "runner_id": manager.runner_id,
                "runner_host": manager.runner_host,
                "_gpu_assignment": {"gpu_ids": [1]},
            }
        )

    updated = update_task_info(task["dir"], start_new_run)
    with manager._lock:
        current = manager._tasks_by_name[task["name"]]
        manager._apply_info_to_task(current, updated)
        manager._mark_running_locked(task["name"], counts_for_batch=True)

    released = []
    monkeypatch.setattr(manager.gpu_scheduler, "release", released.append)
    old_future = Future()
    old_future.set_exception(RuntimeError("old worker failed"))

    manager._on_task_done(
        old_future,
        task["name"],
        expected_runner_id=manager.runner_id,
        expected_run_index=1,
    )

    current = manager.get_task(task["name"])
    assert current["status"] == "running"
    assert current["run_index"] == 2
    assert task["name"] in manager._running_ids
    assert released == []


def test_task_manager_worker_callback_preserves_run_started_during_read(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    manager._sync_status_to_disk("alpha", "running", run_index=1)
    update_task_info(task["dir"], lambda info: info.update({"status": "completed"}))
    original_load = task_manager_module.load_task_metadata

    def load_then_restart(task_dir, **kwargs):
        stale_info = original_load(task_dir, **kwargs)
        manager._sync_status_to_disk("alpha", "running", run_index=2)
        return stale_info

    released = []
    monkeypatch.setattr(task_manager_module, "load_task_metadata", load_then_restart)
    monkeypatch.setattr(manager.gpu_scheduler, "release", released.append)
    future = Future()
    future.set_result(None)

    manager._on_task_done(
        future,
        "alpha",
        expected_runner_id=manager.runner_id,
        expected_run_index=1,
    )

    current = manager.get_task("alpha")
    assert current["status"] == "running"
    assert current["run_index"] == 2
    assert "alpha" in manager._batch_running_ids
    assert released == []


@pytest.mark.parametrize("failure", [PermissionError("read-only metadata"), TimeoutError("metadata lock busy")])
def test_task_manager_worker_error_releases_resources_when_final_write_fails(tmp_path, monkeypatch, failure):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    assert manager._sync_status_to_disk("alpha", "running", run_index=1)
    update_task_info(task["dir"], lambda info: info.update({"_gpu_assignment": {"gpu_ids": [0]}}))

    def fail_final_write(*args, **kwargs):
        raise failure

    released = []
    monkeypatch.setattr(manager, "_mark_failed_on_disk", fail_final_write)
    monkeypatch.setattr(manager.gpu_scheduler, "release", released.append)
    future = Future()
    future.set_exception(RuntimeError("worker failed"))

    manager._on_task_done(
        future,
        "alpha",
        expected_runner_id=manager.runner_id,
        expected_run_index=1,
    )

    assert "alpha" not in manager._running_ids
    assert "alpha" not in manager._batch_running_ids
    assert manager.is_processing is False
    assert released == ["alpha"]
    current = manager.get_task("alpha")
    assert current["status"] == "failed"
    assert str(failure) in current["_load_error"]
    assert load_task_info(task["dir"])["status"] == "running"
    manager._sync_gpu_reservations_from_running_tasks()
    assert "alpha" not in manager.gpu_scheduler._reservations


@pytest.mark.parametrize("concurrent_change", ["notes", "new_run"])
def test_task_manager_final_write_failure_keeps_concurrent_updates(tmp_path, monkeypatch, concurrent_change):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    assert manager._sync_status_to_disk("alpha", "running", run_index=1)

    def change_then_fail(*args, **kwargs):
        if concurrent_change == "notes":
            assert manager.update_task_notes("alpha", "latest notes", expected_notes="")[0]
        else:
            update_task_info(task["dir"], lambda info: info.update({"status": "completed"}))
            assert manager._sync_status_to_disk("alpha", "running", run_index=2)
        raise TimeoutError("metadata lock busy")

    monkeypatch.setattr(manager, "_mark_failed_on_disk", change_then_fail)
    future = Future()
    future.set_exception(RuntimeError("worker failed"))
    manager._on_task_done(
        future,
        "alpha",
        expected_runner_id=manager.runner_id,
        expected_run_index=1,
    )

    current = manager.get_task("alpha")
    if concurrent_change == "notes":
        assert current["notes"] == "latest notes"
        assert current["status"] == "failed"
        assert "alpha" not in manager._running_ids
    else:
        assert current["run_index"] == 2
        assert current["status"] == "running"
        assert "alpha" in manager._running_ids


def test_task_manager_stale_reconciliation_preserves_new_local_run(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    manager.owns_task_lifecycle = True
    assert manager._sync_status_to_disk("alpha", "running", run_index=1)
    stale_info = load_task_info(task["dir"])
    with manager._lock:
        manager._clear_running_locked("alpha")
    original_mark_failed = manager._mark_failed_on_disk

    def restart_then_mark(*args, **kwargs):
        update_task_info(task["dir"], lambda info: info.update({"status": "completed"}))
        assert manager._sync_status_to_disk("alpha", "running", run_index=2)
        return original_mark_failed(*args, **kwargs)

    monkeypatch.setattr(manager, "_mark_failed_on_disk", restart_then_mark)
    info, changed = manager._fail_unowned_running_info_if_needed("alpha", task["dir"], stale_info)

    assert changed is False
    assert info["status"] == "running"
    assert info["run_index"] == 2
    assert load_task_info(task["dir"])["status"] == "running"


@pytest.mark.parametrize("reload_method", ["refresh", "load_task_by_name", "scan_disk"])
def test_task_manager_retries_failed_completion_when_storage_recovers(tmp_path, monkeypatch, reload_method):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    manager.owns_task_lifecycle = True
    assert manager._sync_status_to_disk("alpha", "running", run_index=1)
    original_mark_failed = manager._mark_failed_on_disk

    def fail_final_write(*args, **kwargs):
        raise PermissionError("read-only metadata")

    monkeypatch.setattr(manager, "_mark_failed_on_disk", fail_final_write)
    future = Future()
    future.set_exception(RuntimeError("worker failed"))
    manager._on_task_done(
        future,
        "alpha",
        expected_runner_id=manager.runner_id,
        expected_run_index=1,
    )

    if reload_method == "scan_disk":
        manager.scan_disk()
    elif reload_method == "load_task_by_name":
        manager.load_task_by_name("alpha")
    else:
        manager.refresh_from_disk(check_all=True)
    assert manager.get_task("alpha")["status"] == "failed"
    assert "read-only metadata" in manager.get_task("alpha")["_load_error"]
    assert manager.get_task("alpha")["run_statuses"] == ["failed"]
    assert "_terminal_write_pending" not in manager.get_task("alpha")
    with pytest.raises(PermissionError, match="read-only metadata"):
        manager.refresh_from_disk(raise_on_error=True)

    monkeypatch.setattr(manager, "_mark_failed_on_disk", original_mark_failed)
    manager.refresh_from_disk()

    assert load_task_info(task["dir"])["status"] == "failed"
    assert manager.get_task("alpha")["status"] == "failed"
    assert manager.get_task("alpha")["_load_error"] == ""


def test_task_manager_worker_completion_does_not_wait_for_a_long_metadata_lock(tmp_path):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    manager.owns_task_lifecycle = True
    assert manager._sync_status_to_disk("alpha", "running", run_index=1)
    future = Future()
    future.set_exception(RuntimeError("worker failed"))
    finished = threading.Event()

    def complete_worker():
        manager._on_task_done(
            future,
            "alpha",
            expected_runner_id=manager.runner_id,
            expected_run_index=1,
        )
        finished.set()

    with task_manager_module.task_info_lock(task["dir"]):
        callback = threading.Thread(target=complete_worker)
        callback.start()
        finished_while_locked = finished.wait(timeout=2.0)
    callback.join(timeout=5)

    assert finished_while_locked is True
    assert "alpha" not in manager._batch_running_ids
    manager.refresh_from_disk()
    assert load_task_info(task["dir"])["status"] == "failed"
    assert manager.get_task("alpha")["_load_error"] == ""


@pytest.mark.parametrize("process_started", [False, True])
def test_task_manager_gpu_reservations_keep_starting_or_live_local_run(tmp_path, process_started):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    assert manager._sync_status_to_disk("alpha", "running", run_index=1)

    def assign_gpu(info):
        info["_gpu_assignment"] = {"gpu_ids": [0]}
        if process_started:
            info["pids"] = [os.getpid()]
            info["pid_create_times"] = [psutil.Process().create_time()]

    update_task_info(task["dir"], assign_gpu)
    if process_started:
        with manager._lock:
            manager._clear_running_locked("alpha")

    manager._sync_gpu_reservations_from_running_tasks()

    assert manager.gpu_scheduler._reservations == {"alpha": [0]}


@pytest.mark.parametrize("metadata_state", ["missing", "corrupt"])
@pytest.mark.parametrize("current_run_index", [1, 2])
def test_task_manager_worker_done_releases_slot_when_metadata_unreadable(
    tmp_path, monkeypatch, metadata_state, current_run_index,
):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    assert manager._sync_status_to_disk("alpha", "running", run_index=1)
    if current_run_index == 2:
        update_task_info(task["dir"], lambda info: info.update({"status": "completed"}))
        assert manager._sync_status_to_disk("alpha", "running", run_index=2)
    info_path = Path(task["dir"]) / TASK_INFO_FILENAME
    if metadata_state == "missing":
        info_path.unlink()
    else:
        info_path.write_text("{broken", encoding="utf-8")
    released = []
    monkeypatch.setattr(manager.gpu_scheduler, "release", released.append)
    future = Future()
    future.set_exception(ValueError("Could not persist the final task state"))

    manager._on_task_done(
        future,
        "alpha",
        expected_runner_id=manager.runner_id,
        expected_run_index=1,
    )

    if current_run_index == 2:
        assert "alpha" in manager._batch_running_ids
        assert manager.get_task("alpha")["status"] == "running"
        assert released == []
        return

    assert "alpha" not in manager._running_ids
    assert "alpha" not in manager._batch_running_ids
    assert manager.is_processing is False
    assert released == ["alpha"]
    current = manager.get_task("alpha")
    assert current["status"] == "failed"
    assert "metadata" in current["_load_error"].lower()
    if metadata_state == "corrupt":
        assert info_path.read_text(encoding="utf-8") == "{broken"
    else:
        assert not info_path.exists()


@pytest.mark.parametrize("remove_during_read", [False, True])
def test_task_manager_worker_done_releases_slot_after_full_scan_removes_task(
    tmp_path, monkeypatch, remove_during_read,
):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    manager = _make_task_manager(tasks_dir, owns_task_lifecycle=False)
    manager._sync_status_to_disk("alpha", "running", run_index=1)

    def remove_task():
        shutil.rmtree(task["dir"])
        manager.scan_disk()
        assert manager.get_task("alpha") is None

    if remove_during_read:
        original_load = task_manager_module.load_task_metadata

        def load_then_remove(task_dir, **kwargs):
            info = original_load(task_dir, **kwargs)
            remove_task()
            return info

        monkeypatch.setattr(task_manager_module, "load_task_metadata", load_then_remove)
    else:
        remove_task()
    released = []
    monkeypatch.setattr(manager.gpu_scheduler, "release", released.append)
    future = Future()
    future.set_result(None)

    manager._on_task_done(
        future,
        "alpha",
        expected_runner_id=manager.runner_id,
        expected_run_index=1,
    )

    assert "alpha" not in manager._running_ids
    assert "alpha" not in manager._batch_running_ids
    assert manager.is_processing is False
    assert released == ["alpha"]


def test_task_manager_shutdown_retries_cleanup_before_unregistering_atexit(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    manager = TaskManager(
        tasks_dir=str(tasks_dir),
        lazy_scan=False,
        owns_task_lifecycle=False,
    )
    manager.owns_task_lifecycle = True
    manager._atexit_registered = True

    class RetryLock:
        def __init__(self, lock):
            self.lock = lock
            self.attempts = 0

        def acquire(self, *args, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                return False
            return self.lock.acquire(*args, **kwargs)

        def release(self):
            self.lock.release()

        def __enter__(self):
            self.lock.acquire()
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.lock.release()

    retry_lock = RetryLock(manager._lock)
    manager._lock = retry_lock
    unregistered = []
    monkeypatch.setattr(task_manager_module.atexit, "unregister", unregistered.append)

    manager.shutdown()

    assert manager._shutdown_cleanup_done is False
    assert manager._atexit_registered is True
    assert unregistered == []

    manager.shutdown()

    assert retry_lock.attempts == 2
    assert manager._shutdown_cleanup_done is True
    assert manager._atexit_registered is False
    assert unregistered == [manager._atexit_callback]


def test_task_manager_shutdown_does_not_mark_in_progress_cleanup_complete(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    manager = TaskManager(
        tasks_dir=str(tasks_dir),
        lazy_scan=False,
        owns_task_lifecycle=False,
    )
    manager.owns_task_lifecycle = True
    manager._atexit_registered = True

    cleanup_started = threading.Event()
    release_cleanup = threading.Event()

    def blocking_cleanup():
        cleanup_started.set()
        assert release_cleanup.wait(timeout=2)
        return True

    unregistered = []
    monkeypatch.setattr(manager, "_perform_shutdown_cleanup", blocking_cleanup)
    monkeypatch.setattr(task_manager_module.atexit, "unregister", unregistered.append)

    first = threading.Thread(target=manager.shutdown)
    first.start()
    assert cleanup_started.wait(timeout=2)

    manager.shutdown()

    assert manager._shutdown_cleanup_in_progress is True
    assert manager._shutdown_cleanup_done is False
    assert manager._atexit_registered is True
    assert unregistered == []

    release_cleanup.set()
    first.join(timeout=2)
    assert not first.is_alive()
    assert manager._shutdown_cleanup_in_progress is False
    assert manager._shutdown_cleanup_done is True
    assert manager._atexit_registered is False
    assert unregistered == [manager._atexit_callback]


def test_task_manager_shutdown_keeps_failed_cleanup_retryable(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    manager = TaskManager(
        tasks_dir=str(tasks_dir),
        lazy_scan=False,
        owns_task_lifecycle=False,
    )
    manager.owns_task_lifecycle = True
    manager._atexit_registered = True

    attempts = 0

    def flaky_cleanup():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("cleanup failed")
        return True

    unregistered = []
    monkeypatch.setattr(manager, "_perform_shutdown_cleanup", flaky_cleanup)
    monkeypatch.setattr(task_manager_module.atexit, "unregister", unregistered.append)

    manager.shutdown()

    assert manager._shutdown_cleanup_in_progress is False
    assert manager._shutdown_cleanup_done is False
    assert manager._atexit_registered is True
    assert unregistered == []

    manager.shutdown()

    assert attempts == 2
    assert manager._shutdown_cleanup_done is True
    assert manager._atexit_registered is False
    assert unregistered == [manager._atexit_callback]


@pytest.mark.parametrize("wall_clock_shift", [-3600.0, 3600.0])
def test_task_manager_scheduler_refresh_ignores_wall_clock_adjustments(tmp_path, monkeypatch, wall_clock_shift):
    manager = _make_task_manager(tmp_path / "tasks", owns_task_lifecycle=False)
    manager.acquire_reactive_watch()
    wall_now = [100_000.0]
    elapsed_now = [100.0]
    clock = MagicMock(wraps=time)
    clock.time.side_effect = lambda: wall_now[0]
    clock.monotonic.side_effect = lambda: elapsed_now[0]
    monkeypatch.setattr(task_manager_module, "time", clock)
    refreshes = []
    updates = []

    def refresh(**kwargs):
        assert kwargs == {"check_all": True, "discover": True}
        refreshes.append(elapsed_now[0])
        return True

    waits = 0

    def advance_clock(_timeout):
        nonlocal waits
        waits += 1
        if waits == 1:
            wall_now[0] += wall_clock_shift
            elapsed_now[0] += 0.2
        elif waits == 2:
            wall_now[0] += 1.0
            elapsed_now[0] += 1.0
        else:
            manager._shutdown_event.set()
            return True
        return False

    monkeypatch.setattr(manager, "refresh_from_disk", refresh)
    monkeypatch.setattr(manager, "trigger_update", lambda: updates.append(elapsed_now[0]))
    monkeypatch.setattr(manager, "_refresh_queued_runner_leases", lambda: None)
    monkeypatch.setattr(manager, "_process_cancel_requests", lambda: None)
    monkeypatch.setattr(manager._shutdown_event, "wait", advance_clock)

    manager._scheduler_loop()

    assert refreshes == pytest.approx([100.0, 101.2])
    assert updates == pytest.approx([100.0, 101.2])


def test_task_manager_scheduler_helpers_and_cleanup_edges(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    generator.create_task("queued", {"value": 1})
    generator.create_task("running", {"value": 2})
    generator.create_task("remote", {"value": 3})

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        manager._tasks_by_name["queued"]["status"] = "queued"
        manager._tasks_by_name["queued"]["run_index"] = 1
        manager._tasks_by_name["queued"]["start_times"] = ["2026-01-01_00-00-00"]
        manager._tasks_by_name["queued"]["finish_times"] = ["2026-01-01_00-00-01"]
        manager._tasks_by_name["running"]["status"] = "running"
        manager._tasks_by_name["running"]["run_index"] = 1
        manager._mark_running_locked("running", counts_for_batch=True)
        manager._tasks_by_name["remote"]["status"] = "running"
        manager._tasks_by_name["remote"]["run_index"] = 1
        manager._mark_running_locked("remote", counts_for_batch=False)
        manager._recompute_processing_flag_locked()

    picked, run_index = manager._pick_queued_task()
    assert picked["name"] == "queued"
    assert run_index == 2
    assert "queued" in manager._running_ids

    class ExistingExecutor:
        def __init__(self):
            self.shutdown_calls = []

        def shutdown(self, **kwargs):
            self.shutdown_calls.append(kwargs)

    old_executor = ExistingExecutor()
    manager._executor = old_executor
    manager._executor_workers = 1
    manager.max_workers = 1
    manager._ensure_executor()
    assert manager._executor is old_executor

    manager.max_workers = 2
    manager._ensure_executor()
    assert old_executor.shutdown_calls == [{"wait": False}]
    assert manager._executor is not old_executor

    foreign_info = {
        "status": "running",
        "run_index": 1,
        "runner_id": "remote-runner",
        "runner_host": "other",
        "lease_until": time.time() + 60,
    }
    local_info = {
        "status": "running",
        "runner_id": manager.runner_id,
        "runner_host": manager.runner_host,
        "lease_until": time.time() + 60,
    }

    def fake_load_task_info(task_dir):
        name = Path(task_dir).name
        if name == "remote":
            return foreign_info
        return {**local_info, "run_index": 2 if name == "queued" else 1}

    monkeypatch.setattr("pyruns.core.task_manager.load_task_metadata", fake_load_task_info)
    killed = []
    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda pid, expected_create_time=None: killed.append(pid) or True,
    )
    monkeypatch.setattr(manager, "_current_process_identity", lambda info: (4321, 1000.0))
    monkeypatch.setattr(
        manager,
        "_mark_failed_on_disk",
        lambda task, **kwargs: task.update(status="failed", cleaned=kwargs),
    )

    manager._cleanup_on_shutdown()
    manager._cleanup_on_shutdown()

    assert killed == [4321, 4321]
    assert manager.get_task("running")["status"] == "failed"
    assert manager.get_task("remote")["status"] == "running"
    assert "running" not in manager._batch_running_ids
    assert manager._shutdown_cleanup_done is True


def test_task_manager_scan_async_and_disk_discovery_edge_paths(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    generator.create_task("keep", {"value": 1})
    stale = generator.create_task("stale", {"value": 2})

    manager = _make_task_manager(tasks_dir)

    callbacks = []

    def callback():
        callbacks.append("changed")

    manager.on_change(callback)
    manager.off_change(lambda: None)
    manager.scan_disk_async()
    deadline = time.time() + 2.0
    while not callbacks and time.time() < deadline:
        time.sleep(0.01)
    assert callbacks

    assert manager.load_task_by_name("../bad") is None
    assert "keep" in manager._list_task_dir_names()

    shutil.rmtree(stale["dir"])
    for index in range(9):
        generator.create_task(f"new-{index}", {"value": index})

    changed = manager.sync_task_dirs_from_disk()
    assert changed is True
    assert manager.get_task("stale") is None
    assert manager.get_task("new-0") is not None

    def fail_scandir(_path):
        raise OSError("cannot scan")

    monkeypatch.setattr(task_manager_module.os, "scandir", fail_scandir)
    assert manager._scan_task_dir_names() == (False, [])
    assert manager.sync_task_dirs_from_disk() is False

    missing_root = tmp_path / "missing-root"
    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        missing_manager = TaskManager(tasks_dir=str(missing_root), lazy_scan=False)
    assert missing_manager.list_tasks() == []

    monkeypatch.undo()
    shutil.rmtree(tasks_dir)
    assert manager.sync_task_dirs_from_disk() is True
    assert manager.list_tasks() == []


def test_task_manager_rejects_symlinked_tasks_root(tmp_path):
    outside = tmp_path / "outside-tasks"
    outside.mkdir()
    tasks_link = tmp_path / "tasks"
    try:
        tasks_link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(ValueError, match="Tasks directory must not be"):
        TaskManager(tasks_dir=str(tasks_link), lazy_scan=None)


def test_log_writers_reject_symlinked_run_logs_directory(tmp_path):
    tasks_dir = tmp_path / "tasks"
    task_dir = tasks_dir / "safe"
    task_dir.mkdir(parents=True)
    outside_logs = tmp_path / "outside-logs"
    outside_logs.mkdir()
    victim = outside_logs / "run1.log"
    victim.write_text("keep\n", encoding="utf-8")
    run_logs = task_dir / RUN_LOGS_DIR
    try:
        run_logs.symlink_to(outside_logs, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation unavailable: {exc}")

    with pytest.raises(ValueError, match="Run logs directory must not be"):
        executor._get_log_path(str(task_dir), 1)
    with pytest.raises(ValueError, match="Run logs directory must not be"):
        executor._append_error_summary(
            str(task_dir),
            run_index=1,
            title="blocked",
            detail_lines=["do not write outside"],
        )

    manager = _make_task_manager(tasks_dir)
    try:
        task = {"name": "safe", "dir": str(task_dir), "run_index": 1}
        manager._append_gpu_queue_log(task, "Queued", ["waiting"])
        manager._append_error_summary(
            str(task_dir),
            title="blocked",
            detail_lines=["do not write outside"],
        )
    finally:
        manager.shutdown()

    assert victim.read_text(encoding="utf-8") == "keep\n"
    assert not (outside_logs / "queue.log").exists()
    assert not (outside_logs / ERROR_LOG_FILENAME).exists()


def test_task_manager_default_root_and_lease_edges(tmp_path, monkeypatch):
    custom_root = tmp_path / "run-root"
    tasks_dir = custom_root / TASKS_DIR
    tasks_dir.mkdir(parents=True)

    monkeypatch.setattr("pyruns._config.ROOT_DIR", str(custom_root))
    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=None, lazy_scan=None)

    assert manager.tasks_dir == str(tasks_dir)
    assert manager._disk_scan_complete is False
    assert manager.list_tasks() == []

    assert TaskManager._lease_until_value({"lease_until": "bad"}) == 0.0


def test_task_manager_logs_and_gpu_helper_branches(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "task"
    task_dir.mkdir()
    task = {"name": "task", "dir": str(task_dir), "run_index": 1}

    manager = _make_task_manager(tasks_dir)

    assert manager._format_duration(3660) == "1.0h"
    assert manager._format_duration(120) == "2m"
    assert manager._format_duration(90) == "1.5m"
    assert manager._format_elapsed(3661) == "01:01:01"

    config = GpuSchedulerConfig(
        enabled=True, task_mode="multi", gpus_per_task=2,
        device_ids=[1, 3], max_wait_seconds=3600, stable_seconds=10,
    )
    assert manager._gpu_need_label(config) == "2 GPUs"
    assert manager._gpu_pool_label(config) == "1,3"

    assignment = GpuAssignment(
        task_name="task",
        run_index=1,
        gpu_ids=[1, 3],
        cuda_visible_devices="1,3",
        env={"CUDA_VISIBLE_DEVICES": "1,3"},
        waited_seconds=61,
    )
    assert manager._gpu_assignment_to_dict(assignment)["gpu_ids"] == [1, 3]

    with patch("pyruns.core.task_manager.time.monotonic", return_value=90.0):
        manager._append_gpu_wait_started(task, 1, config)
    manager._append_gpu_wait_decision(
        task,
        1,
        config,
        GpuDecision(assignment=None, reason="busy", snapshot=[]),
        waited=10,
        now=100,
    )
    manager._append_gpu_assignment(task, assignment)
    manager._append_gpu_assignment(
        task,
        GpuAssignment(
            task_name="task",
            run_index=2,
            gpu_ids=[],
            cuda_visible_devices="GPU-uuid-0,MIG-GPU-uuid/0/1",
            env={"PYRUNS_ASSIGNED_GPUS": "GPU-uuid-0,MIG-GPU-uuid/0/1"},
            waited_seconds=0,
        ),
    )
    repeated = manager._gpu_wait_decision_lines(
        task,
        1,
        config,
        GpuDecision(assignment=None, reason="busy", snapshot=[]),
        waited=11,
        now=101,
    )
    assert repeated is None
    periodic = manager._gpu_wait_decision_lines(
        task,
        1,
        config,
        GpuDecision(assignment=None, reason="busy", snapshot=[]),
        waited=61,
        now=161,
    )
    assert periodic is not None
    assert "still waiting after 00:01:01" in periodic[0]

    queue_log = task_dir / RUN_LOGS_DIR / "queue.log"
    queue_bytes = queue_log.read_bytes()
    with open(queue_log, "r", encoding="utf-8", newline="") as handle:
        queue_text = handle.read()
    refresh_line = (
        "\r[PYRUNS] Run #1 still waiting after 00:00:10 | "
        "blocked: busy | GPU snapshot: no NVIDIA GPU metrics available"
    )
    assert refresh_line.encode("utf-8") in queue_bytes
    assert refresh_line in queue_text
    assert "\n" + refresh_line in queue_text
    assert "[PYRUNS] [GPU WAIT] Run #1 still waiting after 00:00:10" not in queue_text
    assert "[PYRUNS]   Updated at " in queue_text
    assert "-------------------- RUN #2 --------------------" in queue_text
    run_one_text, run_two_text = queue_text.split("-------------------- RUN #2 --------------------", 1)
    assert not run_two_text.startswith("\n\n")
    run_two_body = run_two_text.lstrip("\n")
    assert "\n\n[PYRUNS] Last status at " not in run_one_text
    assert "\n\n[PYRUNS] [GPU ASSIGNED]" in run_one_text
    assert refresh_line + "\n\n[PYRUNS] [GPU ASSIGNED] Run #1 assigned GPUs 1,3 after 00:01:01" in run_one_text
    assert "\n\n[PYRUNS] -------------------- RUN #2 --------------------" in queue_text
    assert run_two_body.startswith("[PYRUNS] [GPU ASSIGNED]")
    assert "\n\n[PYRUNS] Last status at " not in run_two_body
    assert "GPU WAIT" in queue_text
    assert "GPU ASSIGNED" in queue_text
    assert "still waiting after 00:00:10" in queue_text
    assert "still waiting after 00:01:01" not in queue_text
    assert "CUDA_VISIBLE_DEVICES=GPU-uuid-0,MIG-GPU-uuid/0/1" in queue_text
    assert "Updated at " in queue_text
    assert "Last status at " not in queue_text

    class FakeGpu:
        def __init__(self, index, memory_used_pct, compute_util_pct, free_memory_gb):
            self.index = index
            self.memory_used_pct = memory_used_pct
            self.compute_util_pct = compute_util_pct
            self.free_memory_gb = free_memory_gb

    assert manager._gpu_snapshot_lines([], config) == ["GPU snapshot: no NVIDIA GPU metrics available"]
    assert manager._gpu_snapshot_lines([FakeGpu(4, 90, 50, 1)], config) == ["GPU snapshot: configured GPU pool is empty"]
    visible_lines = manager._gpu_snapshot_lines(
        [FakeGpu(1, 10, 5, 80), FakeGpu(3, 90, 50, 1)],
        config,
    )
    assert "GPU 1 eligible" in visible_lines[0]
    assert "GPU 3 blocked" in visible_lines[1]

    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no write")))
    manager._append_gpu_queue_log(task, "GPU WAIT", ["cannot write"])
    manager._append_error_summary(str(task_dir), title="error", detail_lines=["detail"])


def test_task_manager_gpu_wait_log_interval_uses_stable_window(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    manager = _make_task_manager(tasks_dir)

    task = {"name": "task", "dir": str(tasks_dir / "task"), "run_index": 1}
    config = GpuSchedulerConfig(enabled=True, stable_seconds=15)
    decision = GpuDecision(assignment=None, reason="busy", snapshot=[])

    assert manager._gpu_wait_log_interval(config) == 15

    first = manager._gpu_wait_decision_lines(task, 1, config, decision, waited=0, now=100)
    assert first is not None
    assert manager._gpu_wait_decision_lines(task, 1, config, decision, waited=14, now=114) is None
    second = manager._gpu_wait_decision_lines(task, 1, config, decision, waited=15, now=115)
    assert second is not None
    assert "still waiting after 00:00:15" in second[0]


def test_task_manager_gpu_wait_refresh_labels_stabilizing_candidates():
    line = TaskManager._gpu_wait_refresh_line(
        [
            "Run #3 still waiting after 00:00:05",
            "Stabilizing: GPU 0 stabilizing 0.0/5s",
            "GPU 0 eligible: memory 58%, compute 2%, free 5.1 GiB",
        ]
    )

    assert line == (
        "[PYRUNS] Run #3 still waiting after 00:00:05 | "
        "stabilizing: GPU 0 stabilizing 0.0/5s | "
        "GPU 0 eligible: memory 58%, compute 2%, free 5.1 GiB"
    )
    assert "blocked:" not in line


def test_task_manager_gpu_wait_log_interval_ignores_reason_changes_until_stable_window(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    manager = _make_task_manager(tasks_dir)

    task = {"name": "task", "dir": str(tasks_dir / "task"), "run_index": 1}
    config = GpuSchedulerConfig(enabled=True, stable_seconds=15)
    first_decision = GpuDecision(assignment=None, reason="GPU 0 memory 22% > 20%", snapshot=[])
    changed_decision = GpuDecision(assignment=None, reason="GPU 0 memory 24% > 20%", snapshot=[])

    assert manager._gpu_wait_log_interval(config) == 15

    first = manager._gpu_wait_decision_lines(task, 1, config, first_decision, waited=0, now=100)
    assert first is not None
    assert manager._gpu_wait_decision_lines(task, 1, config, changed_decision, waited=1, now=101) is None
    second = manager._gpu_wait_decision_lines(task, 1, config, changed_decision, waited=15, now=115)
    assert second is not None
    assert "still waiting after 00:00:15" in second[0]


def test_launcher_path_helpers_and_native_picker_fallbacks(tmp_path, monkeypatch):
    import pyruns.launcher as launcher

    script_path = tmp_path / "_shell_.py"
    script_path.write_text("print('shell named script')\n", encoding="utf-8")
    not_script = tmp_path / "not_script.txt"
    not_script.write_text("", encoding="utf-8")

    assert launcher.normalize_path(str(script_path)).endswith("_shell_.py")
    assert launcher.validate_python_script_path(str(script_path)).endswith("_shell_.py")
    with pytest.raises(FileNotFoundError):
        launcher.validate_python_script_path(str(not_script))
    assert launcher.workspace_name_for_script_base(SHELL_WORKSPACE_NAME) == f"py{SHELL_WORKSPACE_NAME}"
    assert launcher.workspace_root_for_script(str(script_path)).endswith(f"{DEFAULT_ROOT_NAME}/py{SHELL_WORKSPACE_NAME}")
    assert launcher.shell_workspace_root_for_run_root(str(tmp_path / DEFAULT_ROOT_NAME)).endswith(f"{DEFAULT_ROOT_NAME}/{SHELL_WORKSPACE_NAME}")
    assert launcher.shell_workspace_root_for_run_root(str(tmp_path / DEFAULT_ROOT_NAME / SHELL_WORKSPACE_NAME)).endswith(SHELL_WORKSPACE_NAME)
    assert launcher.shell_project_root_for_workspace(str(tmp_path / DEFAULT_ROOT_NAME / SHELL_WORKSPACE_NAME)) == str(tmp_path).replace("\\", "/")

    monkeypatch.setattr(launcher.os, "name", "posix")
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert launcher.native_picker_available() is False
    monkeypatch.setenv("DISPLAY", ":1")
    assert launcher.native_picker_available() is True

    original_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "tkinter" or name.startswith("tkinter"):
            raise ImportError("tk unavailable")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)
    assert launcher.choose_script_file(str(tmp_path)) is None
    assert launcher.choose_config_file(str(tmp_path)) is None
    assert launcher.choose_shell_file(str(tmp_path)) is None
    assert launcher.choose_directory(str(tmp_path)) is None


def test_launcher_native_picker_success_paths_normalize_selection(tmp_path, monkeypatch):
    import types
    import pyruns.launcher as launcher

    script = tmp_path / "train.py"
    config = tmp_path / "config.yaml"
    shell = tmp_path / "run.sh"
    directory = tmp_path / "workspace"
    for path in (script, config, shell):
        path.write_text("", encoding="utf-8")
    directory.mkdir()

    selected_files = iter([str(script), str(config), str(shell)])
    roots = []

    class FakeRoot:
        def __init__(self):
            self.withdrawn = False
            self.destroyed = False
            self.attributes_calls = []

        def withdraw(self):
            self.withdrawn = True

        def attributes(self, *args):
            self.attributes_calls.append(args)

        def destroy(self):
            self.destroyed = True

    def make_root():
        root = FakeRoot()
        roots.append(root)
        return root

    filedialog = types.SimpleNamespace(
        askopenfilename=lambda **kwargs: next(selected_files),
        askdirectory=lambda **kwargs: str(directory),
    )
    tkinter = types.SimpleNamespace(Tk=make_root, filedialog=filedialog)
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", filedialog)

    assert launcher.choose_script_file(str(tmp_path)) == str(script).replace("\\", "/")
    assert launcher.choose_config_file(str(tmp_path)) == str(config).replace("\\", "/")
    assert launcher.choose_shell_file(str(tmp_path)) == str(shell).replace("\\", "/")
    assert launcher.choose_directory(str(tmp_path)) == str(directory).replace("\\", "/")
    assert len(roots) == 4
    assert all(root.withdrawn and root.destroyed for root in roots)
    assert all(root.attributes_calls == [("-topmost", True)] for root in roots)


def test_launcher_discovers_workspace_and_file_candidates(tmp_path):
    import pyruns.launcher as launcher

    project = tmp_path / "project"
    project.mkdir()
    script = project / "train.py"
    script.write_text("print('train')\n", encoding="utf-8")
    file_only = project / "eval.py"
    file_only.write_text("print('eval')\n", encoding="utf-8")
    workspace_root = project / DEFAULT_ROOT_NAME
    workspace = workspace_root / "train"
    workspace.mkdir(parents=True)
    (workspace / SCRIPT_INFO_FILENAME).write_text(
        json.dumps(
            {
                "workspace_kind": "script",
                "script_name": "train",
                "script_path": str(script),
            }
        ),
        encoding="utf-8",
    )
    shell_workspace = workspace_root / SHELL_WORKSPACE_NAME
    shell_workspace.mkdir()
    (shell_workspace / SCRIPT_INFO_FILENAME).write_text(json.dumps({"workspace_kind": WORKSPACE_KIND_SHELL}), encoding="utf-8")
    bad_workspace = workspace_root / "bad"
    bad_workspace.mkdir()
    (bad_workspace / SCRIPT_INFO_FILENAME).write_text("{bad json", encoding="utf-8")

    assert launcher.resolve_workspace_for_script(str(script)) == str(workspace).replace("\\", "/")
    candidates = launcher.list_script_candidates(str(project))
    by_name = {item["script_name"]: item for item in candidates}

    assert by_name["train"]["source"] == "workspace+file"
    assert by_name["eval"]["source"] == "file"
    assert SHELL_WORKSPACE_NAME not in by_name

    summary = launcher.read_workspace_summary(str(workspace))
    assert summary["script_name"] == "train"
    assert summary["workspace_kind"] == "script"


def test_launcher_config_candidates_bootstrap_errors_and_query(tmp_path, monkeypatch, capsys):
    import pyruns.launcher as launcher

    script = tmp_path / "train.py"
    script.write_text("import pyruns\ncfg = pyruns.load()\n", encoding="utf-8")
    config = tmp_path / "configs" / "base.yaml"
    config.parent.mkdir()
    config.write_text("lr: 0.1\n", encoding="utf-8")
    root_default = Path(launcher.workspace_root_for_script(str(script))) / CONFIG_DEFAULT_FILENAME
    root_default.parent.mkdir(parents=True)
    root_default.write_text("lr: 0.2\n", encoding="utf-8")

    candidates = launcher.list_config_candidates(str(script))
    labels = [item["label"] for item in candidates]
    assert labels[0] == "Workspace default"
    assert "configs/base.yaml" in labels

    monkeypatch.setattr("pyruns.launcher.detect_config_source_fast", lambda path: ("pyruns_load", None))
    metadata = launcher.get_config_selection_metadata(str(script))
    assert metadata["requires_config_template"] is False

    root_default.unlink()
    metadata = launcher.get_config_selection_metadata(str(script))
    assert metadata["requires_config_template"] is True
    assert launcher.list_workspace_candidates(str(script), str(config))[0]["config_name"] == "base.yaml"

    with pytest.raises(FileNotFoundError, match="Custom config"):
        launcher.bootstrap_workspace(str(script), str(tmp_path / "missing.yaml"))

    with pytest.raises(FileNotFoundError, match="needs a YAML template"):
        launcher.bootstrap_workspace(str(script))

    existing_workspace = Path(launcher.workspace_root_for_script(str(script)))
    existing_workspace.mkdir(parents=True, exist_ok=True)
    (existing_workspace / SCRIPT_INFO_FILENAME).write_text(
        json.dumps(
            {
                "created_at": "2026-01-01 00:00:00",
                "last_used_template": "old",
            }
        ),
        encoding="utf-8",
    )
    root_default.write_text("lr: 0.2\n", encoding="utf-8")
    workspace = launcher.bootstrap_workspace(str(script))
    info = json.loads((Path(workspace) / SCRIPT_INFO_FILENAME).read_text(encoding="utf-8"))
    assert info["last_used_template"] == "old"

    shell_root = launcher.bootstrap_shell_workspace(workspace)
    shell_info = json.loads((Path(shell_root) / SCRIPT_INFO_FILENAME).read_text(encoding="utf-8"))
    assert shell_info["workspace_kind"] == WORKSPACE_KIND_SHELL
    assert Path(shell_root).name == SHELL_WORKSPACE_NAME

    query = launcher.launcher_query(str(script), str(config))
    assert query.startswith("/?launcher=1")
    assert "script=" in query and "config=" in query

    monkeypatch.setattr("pyruns.launcher.bootstrap_workspace", lambda script_path, custom_yaml=None: (_ for _ in ()).throw(FileNotFoundError("missing script")))
    with pytest.raises(SystemExit):
        launcher.bootstrap_from_cli(str(script))
    assert "missing script" in capsys.readouterr().out


def test_run_history_normalization_aligns_process_and_source_metadata():
    meta = {
        "run_index": 2,
        "start_times": ["started"],
        "source_states": ["git one", "git two"],
    }

    assert run_slot_count(meta) == 2
    assert normalize_run_history(meta) == 2
    assert meta["start_times"] == ["started", ""]
    assert meta["durations"] == [None, None]
    assert meta["exit_codes"] == [None, None]
    assert meta["source_states"] == ["git one", "git two"]

    assert ensure_run_slot(meta, 3) == 2
    assert all(len(meta[key]) == 3 for key in (
        "start_times",
        "finish_times",
        "pids",
        "durations",
        "exit_codes",
        "source_states",
        "records",
        "tracks",
    ))
    TaskManager._trim_run_slots(meta, 1)
    assert all(len(meta[key]) == 1 for key in (
        "start_times",
        "finish_times",
        "pids",
        "durations",
        "exit_codes",
        "source_states",
        "records",
        "tracks",
    ))
