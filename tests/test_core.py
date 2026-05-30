"""
Tests for pyruns.core — config_manager, system_metrics, executor,
task_generator, and report.
"""
import csv
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import psutil
import yaml
from unittest.mock import patch, MagicMock

from pyruns._config import (
    ENV_KEY_CONFIG,
    CONFIG_FILENAME,
    POWERSHELL_CONFIG_FILENAME,
    DEFAULT_ROOT_NAME,
    SCRIPT_INFO_FILENAME,
    SHELL_CONFIG_FILENAME,
    SHELL_WORKSPACE_NAME,
    TASK_INFO_FILENAME,
    RECORDS_KEY,
    TASK_KIND_CONFIG,
    TASK_KIND_SHELL,
)
from pyruns.core.config_manager import ConfigNode, ConfigManager
from pyruns.core.executor import _prepare_env, _build_command, run_task_worker
from pyruns.core.report import build_export_csv, build_export_json
from pyruns.core.system_metrics import SystemMonitor
from pyruns.core.task_generator import TaskGenerator, create_task_object
from pyruns.core.task_manager import TaskManager
from pyruns.launcher import (
    bootstrap_shell_workspace,
    bootstrap_workspace,
    list_script_candidates,
    shell_workspace_root_for_run_root,
    workspace_root_for_script,
)
from pyruns.utils.batch_utils import generate_batch_configs
from pyruns.utils.info_io import save_task_info
from pyruns.utils.config_utils import save_yaml
from pyruns.utils.shell_runtime import get_shell_config_filename_for_workspace, get_shell_runtime_for_workspace


def test_prepare_env_allows_child_to_import_current_pyruns_from_script_workdir(tmp_path, monkeypatch):
    """Experiment scripts run from their own cwd but still need pyruns APIs."""
    monkeypatch.delenv("PYTHONPATH", raising=False)

    env = _prepare_env(task_dir=str(tmp_path), task_kind=TASK_KIND_CONFIG)
    result = subprocess.run(
        [sys.executable, "-c", "import pyruns; print(pyruns.__file__)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).is_file()


def test_config_node_init():
    data = {
        "lr": 0.01,
        "optimizer": {
            "name": "adam",
            "beta": 0.9
        },
        "layers": [64, 128, {"dropout": 0.5}],
        "_private": "hidden"
    }
    node = ConfigNode(data)
    
    assert node.lr == 0.01
    assert node.optimizer.name == "adam"
    assert node.optimizer.beta == 0.9
    assert len(node.layers) == 3
    assert node.layers[0] == 64
    assert node.layers[2].dropout == 0.5
    assert getattr(node, "_private") == "hidden"


def test_config_node_to_dict():
    data = {
        "lr": 0.01,
        "optimizer": {
            "name": "adam",
            "beta": 0.9
        },
        "layers": [64, {"dropout": 0.5}],
        "_private": "should be ignored"
    }
    node = ConfigNode(data)
    d = node.to_dict()
    
    assert "lr" in d
    assert "optimizer" in d
    assert isinstance(d["optimizer"], dict)
    assert d["optimizer"]["name"] == "adam"
    assert isinstance(d["layers"], list)
    assert isinstance(d["layers"][1], dict)
    assert d["layers"][1]["dropout"] == 0.5
    assert "_private" not in d


def test_config_node_repr():
    node = ConfigNode({"a": 1, "b": "str"})
    r = repr(node)
    assert "ConfigNode(" in r
    assert "a=1" in r
    assert "b='str'" in r


def test_config_manager_not_loaded():
    cm = ConfigManager()
    with pytest.raises(RuntimeError, match="not loaded"):
        cm.load()


def test_config_manager_file_not_found():
    cm = ConfigManager()
    with pytest.raises(FileNotFoundError):
        cm.read("does_not_exist_at_all.yaml")


def test_config_manager_read_yaml(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("a: 1\nb: 2", encoding="utf-8")
    
    cm = ConfigManager()
    cm.read(str(p))
    node = cm.load()
    assert node.a == 1
    assert node.b == 2


def test_config_manager_read_json(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text('{"a": 1, "b": {"c": 3}}', encoding="utf-8")
    
    cm = ConfigManager()
    cm.read(str(p))
    node = cm.load()
    assert node.a == 1
    assert node.b.c == 3


def test_config_manager_unsupported_format(tmp_path):
    p = tmp_path / "cfg.txt"
    p.write_text("Hello", encoding="utf-8")
    
    cm = ConfigManager()
    with pytest.raises(RuntimeError, match="Unsupported format"):
        cm.read(str(p))


def test_config_manager_read_list(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("- a: 1\n- b: 2", encoding="utf-8")
    
    cm = ConfigManager()
    cm.read(str(p))
    nodes = cm.load()
    assert isinstance(nodes, list)
    assert len(nodes) == 2
    assert nodes[0].a == 1
    assert nodes[1].b == 2


@pytest.fixture
def mock_logger(monkeypatch):
    class MockLogger:
        logs = []
        def info(self, msg, *args):
            self.logs.append(("INFO", msg % args))
        def error(self, msg, *args):
            self.logs.append(("ERROR", msg % args))
    logger = MockLogger()
    monkeypatch.setattr("pyruns.core.config_manager.logger", logger)
    return logger

def test_config_manager_parse_error(tmp_path, mock_logger):
    p = tmp_path / "bad.yaml"
    p.write_text("a: \n  - b:\n c: [invalid yaml", encoding="utf-8")
    
    cm = ConfigManager()
    with pytest.raises(RuntimeError, match="Failed to parse config"):
        cm.read(str(p))
    
    assert any("Failed to parse config" in msg for lvl, msg in mock_logger.logs if lvl == "ERROR")


# ═══════════════════════════════════════════════════════════════
#  SystemMonitor
# ═══════════════════════════════════════════════════════════════

@patch("pyruns.core.system_metrics.psutil")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_system_monitor_sample(mock_subprocess, mock_psutil):
    # Setup CPU/RAM mocks
    mock_psutil.cpu_percent.return_value = 25.5
    mock_mem = MagicMock()
    mock_mem.percent = 60.0
    mock_psutil.virtual_memory.return_value = mock_mem
    mock_psutil.Process.side_effect = [
        MagicMock(username=MagicMock(return_value="alice")),
        MagicMock(username=MagicMock(return_value="bob")),
        MagicMock(username=MagicMock(return_value="carol")),
    ]
    
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
    assert [proc["user"] for proc in gpus[0]["processes"]] == ["alice", "bob"]
    
    assert gpus[1]["index"] == 1
    assert gpus[1]["name"] == "NVIDIA RTX 4080"
    assert gpus[1]["util"] == 90.0
    assert gpus[1]["processes"][0]["name"] == "train.py"
    assert gpus[1]["processes"][0]["user"] == "carol"
    
    assert monitor._gpu_cache == gpus


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
    
    # Error should return cache
    assert metrics["gpus"] == cached_gpus


@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_system_monitor_gpu_empty(mock_subprocess):
    # Setup GPU to return empty (e.g. no GPUs or driver not loaded properly but command succeeds)
    mock_subprocess.side_effect = [b"   \n\n\n", b""]
    
    monitor = SystemMonitor()
    gpus = monitor._get_gpu_metrics()
    assert gpus == []


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


@patch("pyruns.core.system_metrics.psutil.Process")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_system_monitor_gpu_process_user_falls_back_to_unknown(mock_subprocess, mock_process):
    mock_subprocess.return_value = b"GPU-AAA, 1234, python.exe, 2048\n"
    mock_process.side_effect = psutil.AccessDenied(pid=1234)

    monitor = SystemMonitor()
    processes = monitor._get_gpu_processes()

    assert processes["GPU-AAA"][0]["user"] == "unknown"


# ═══════════════════════════════════════════════════════════════
#  Executor
# ═══════════════════════════════════════════════════════════════


def test_prepare_env():
    env1 = _prepare_env(extra_env={"CUDA_VISIBLE_DEVICES": "1"})
    assert env1["PYTHONIOENCODING"] == "utf-8"
    assert env1["CUDA_VISIBLE_DEVICES"] == "1"
    
    env2 = _prepare_env(task_dir="/fake/dir")
    assert env2[ENV_KEY_CONFIG] == os.path.join("/fake/dir", CONFIG_FILENAME)

    env3 = _prepare_env(task_dir="/fake/dir", task_kind=TASK_KIND_SHELL, config_file=SHELL_CONFIG_FILENAME)
    assert ENV_KEY_CONFIG not in env3


def test_prepare_env_prefers_current_python_executable_on_path(monkeypatch):
    stale_path = os.pathsep.join(["/not/current/python", "/another/bin"])
    monkeypatch.setenv("PATH", stale_path)

    env = _prepare_env(task_dir="/fake/dir", task_kind=TASK_KIND_SHELL)

    path_entries = env["PATH"].split(os.pathsep)
    assert path_entries[0] == os.path.dirname(sys.executable)
    assert "/not/current/python" in path_entries


def test_prepare_env_preserves_parent_conda_environment_and_applies_task_overrides(monkeypatch):
    monkeypatch.setenv("CONDA_PREFIX", "/opt/conda/envs/exp")
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "exp")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,1")
    monkeypatch.setenv("PYTHONPATH", "/parent/pythonpath")

    env = _prepare_env(
        extra_env={"CUDA_VISIBLE_DEVICES": "2", "PYRUNS_EXAMPLE_ENV": "task-value"},
        task_dir="/fake/task",
        task_kind=TASK_KIND_CONFIG,
    )

    assert env["CONDA_PREFIX"] == "/opt/conda/envs/exp"
    assert env["CONDA_DEFAULT_ENV"] == "exp"
    assert env["CUDA_VISIBLE_DEVICES"] == "2"
    assert env["PYRUNS_EXAMPLE_ENV"] == "task-value"
    assert "/parent/pythonpath" in env["PYTHONPATH"]
    assert env[ENV_KEY_CONFIG] == os.path.join("/fake/task", CONFIG_FILENAME)


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.parse_utils.extract_argparse_params")
def test_build_command_argparse(mock_extract, mock_detect):
    mock_detect.return_value = ("argparse", None)
    mock_extract.return_value = {
        "lr": {"name": "--lr", "default": 0.01},
        "epochs": {"name": "--epochs", "default": 5},
    }
    
    script_path = "train.py"
    config = {"lr": 0.05, "epochs": 10, "flag": True}
    
    cmd, wd, cleanup_paths = _build_command(None, script_path, None, config)
    
    # sys.executable, train.py, --lr, 0.05, --epochs, 10, --flag
    assert cmd[0] == sys.executable
    assert cmd[1] == "train.py"
    assert "--lr" in cmd
    assert "0.05" in cmd
    assert "--flag" in cmd
    assert cleanup_paths == []


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.parse_utils.extract_argparse_params")
def test_build_command_argparse_uses_declared_flags_and_bool_actions(mock_extract, mock_detect):
    mock_detect.return_value = ("argparse", None)
    mock_extract.return_value = {
        "batch_size": {"name": "--batch-size", "default": 32},
        "use_amp": {"name": "--use-amp", "action": "store_true", "default": False},
        "cache": {"name": "--no-cache", "action": "store_false", "default": True},
    }

    cmd, _, _ = _build_command(
        None,
        "train.py",
        None,
        {"batch_size": 64, "use_amp": True, "cache": False},
    )

    assert "--batch-size" in cmd
    assert "--batch_size" not in cmd
    assert cmd[cmd.index("--batch-size") + 1] == "64"
    assert "--use-amp" in cmd
    assert "--no-cache" in cmd


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.parse_utils.extract_argparse_params")
def test_build_command_argparse_groups_nargs_list_values(mock_extract, mock_detect):
    mock_detect.return_value = ("argparse", None)
    mock_extract.return_value = {
        "layers": {"name": "--layers", "nargs": "+", "default": [64]},
    }

    cmd, _, _ = _build_command(None, "train.py", None, {"layers": [128, 256]})

    assert cmd.count("--layers") == 1
    index = cmd.index("--layers")
    assert cmd[index:index + 3] == ["--layers", "128", "256"]


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.parse_utils.extract_argparse_params")
def test_build_command_argparse_append_with_nargs_repeats_grouped_values(mock_extract, mock_detect):
    mock_detect.return_value = ("argparse", None)
    mock_extract.return_value = {
        "pair": {"name": "--pair", "action": "append", "nargs": 2, "default": []},
    }

    cmd, _, _ = _build_command(
        None,
        "train.py",
        None,
        {"pair": [["train", "dev"], ["test", "holdout"]]},
    )

    assert cmd == [
        sys.executable,
        "train.py",
        "--pair",
        "train",
        "dev",
        "--pair",
        "test",
        "holdout",
    ]


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.parse_utils.extract_argparse_params")
def test_build_command_argparse_boolean_optional_and_typed_bool(mock_extract, mock_detect):
    mock_detect.return_value = ("argparse", None)
    mock_extract.return_value = {
        "compile": {
            "name": "--compile",
            "action": "argparse.BooleanOptionalAction",
            "default": True,
        },
        "enabled": {
            "name": "--enabled",
            "type": "bool",
            "default": True,
        },
    }

    cmd, _, _ = _build_command(
        None,
        "train.py",
        None,
        {"compile": False, "enabled": False},
    )

    assert "--no-compile" in cmd
    assert "--enabled" in cmd
    assert cmd[cmd.index("--enabled") + 1] == "False"


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.parse_utils.extract_argparse_params")
def test_build_command_argparse_count_action_repeats_flag(mock_extract, mock_detect):
    mock_detect.return_value = ("argparse", None)
    mock_extract.return_value = {
        "verbose": {
            "flags": ["-v", "--verbose"],
            "name": "--verbose",
            "action": "count",
            "default": 0,
        },
    }

    cmd, _, _ = _build_command(None, "train.py", None, {"verbose": 2})

    assert cmd.count("--verbose") == 2
    assert "2" not in cmd


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
def test_build_command_non_argparse(mock_detect):
    mock_detect.return_value = ("pyruns_load", None)
    
    script_path = "train.py"
    config = {"lr": 0.05}
    
    cmd, wd, cleanup_paths = _build_command(None, script_path, None, config)
    
    # Should only contain python and script, no args appended
    assert len(cmd) == 2
    assert cmd[0] == sys.executable
    assert cmd[1] == "train.py"
    assert cleanup_paths == []


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
def test_build_command_python_task_uses_script_directory_workdir(mock_detect, tmp_path):
    mock_detect.return_value = ("pyruns_load", None)
    script_dir = tmp_path / "project"
    script_dir.mkdir()
    script_path = script_dir / "train.py"
    script_path.write_text("print('cwd')\n", encoding="utf-8")

    cmd, wd, cleanup_paths = _build_command(None, str(script_path), None, {})

    assert cmd == [sys.executable, str(script_path)]
    assert wd == str(script_dir)
    assert cleanup_paths == []


@patch("pyruns.core.executor._resolve_shell_executable")
def test_build_command_shell_task_posix(mock_shell, tmp_path, monkeypatch):
    monkeypatch.setattr("pyruns.core.executor._is_windows", lambda: False)
    mock_shell.return_value = "/bin/bash"
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    script_path = task_dir / SHELL_CONFIG_FILENAME
    script_path.write_text("echo hello\n", encoding="utf-8")

    cmd, wd, cleanup_paths = _build_command(
        None,
        None,
        None,
        {},
        task_kind=TASK_KIND_SHELL,
        task_dir=str(task_dir),
        config_file=SHELL_CONFIG_FILENAME,
    )

    assert cmd == ["/bin/bash", str(script_path)]
    assert wd == str(task_dir)
    assert cleanup_paths == []


@patch("pyruns.core.executor._resolve_shell_executable")
def test_build_command_shell_task_uses_project_root_workdir(mock_shell, tmp_path, monkeypatch):
    monkeypatch.setattr("pyruns.core.executor._is_windows", lambda: False)
    mock_shell.return_value = "/bin/bash"
    project_root = tmp_path / "project"
    task_dir = project_root / DEFAULT_ROOT_NAME / SHELL_WORKSPACE_NAME / "tasks" / "task"
    task_dir.mkdir(parents=True)
    workspace_dir = task_dir.parents[1]
    (workspace_dir / SCRIPT_INFO_FILENAME).write_text(
        json.dumps({"workspace_kind": "shell", "project_root": str(project_root)}),
        encoding="utf-8",
    )
    script_path = task_dir / SHELL_CONFIG_FILENAME
    script_path.write_text("pwd\n", encoding="utf-8")

    cmd, wd, cleanup_paths = _build_command(
        None,
        None,
        None,
        {},
        task_kind=TASK_KIND_SHELL,
        task_dir=str(task_dir),
        config_file=SHELL_CONFIG_FILENAME,
    )

    assert cmd == ["/bin/bash", str(script_path)]
    assert wd == str(project_root).replace("\\", "/")
    assert cleanup_paths == []


@patch("pyruns.core.executor._resolve_shell_executable")
def test_build_command_shell_task_windows_cmd(mock_shell, tmp_path, monkeypatch):
    monkeypatch.setattr("pyruns.core.executor._is_windows", lambda: True)
    mock_shell.return_value = r"C:\Windows\System32\cmd.exe"
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    script_path = task_dir / SHELL_CONFIG_FILENAME
    script_path.write_text("#!/usr/bin/env bash\necho hello\n", encoding="utf-8")

    cmd, wd, cleanup_paths = _build_command(
        None,
        None,
        None,
        {},
        task_kind=TASK_KIND_SHELL,
        task_dir=str(task_dir),
        config_file=SHELL_CONFIG_FILENAME,
    )

    wrapper_path = Path(cleanup_paths[0])
    assert cmd == [r"C:\Windows\System32\cmd.exe", "/d", "/c", str(wrapper_path)]
    assert wd == str(task_dir)
    assert wrapper_path.exists()
    assert wrapper_path.parent != task_dir
    wrapper_content = wrapper_path.read_text(encoding="utf-8-sig")
    assert "#!/usr/bin/env bash" not in wrapper_content
    assert "echo hello" in wrapper_content
    wrapper_path.unlink()


@patch("pyruns.core.executor._resolve_shell_executable")
def test_build_command_shell_task_windows_powershell(mock_shell, tmp_path, monkeypatch):
    monkeypatch.setattr("pyruns.core.executor._is_windows", lambda: True)
    mock_shell.return_value = r"C:\Program Files\PowerShell\7\pwsh.exe"
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    script_path = task_dir / SHELL_CONFIG_FILENAME
    script_path.write_text("#!/usr/bin/env bash\nWrite-Host 'hello'\n", encoding="utf-8")

    cmd, wd, cleanup_paths = _build_command(
        None,
        None,
        None,
        {},
        task_kind=TASK_KIND_SHELL,
        task_dir=str(task_dir),
        config_file=SHELL_CONFIG_FILENAME,
    )
    wrapper_path = Path(cleanup_paths[0])
    assert cmd == [
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(wrapper_path),
    ]
    assert wd == str(task_dir)
    assert wrapper_path.exists()
    assert wrapper_path.parent != task_dir
    wrapper_content = wrapper_path.read_text(encoding="utf-8-sig")
    assert "#!/usr/bin/env bash" not in wrapper_content
    assert "Write-Host 'hello'" in wrapper_content
    assert "[Console]::OutputEncoding" in wrapper_content
    assert "$OutputEncoding = $__pyrunsUtf8" in wrapper_content
    wrapper_path.unlink()


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


def test_shell_workspace_root_uses_project_root_when_given_pyruns_root(tmp_path):
    project_root = tmp_path / DEFAULT_ROOT_NAME
    project_root.mkdir(parents=True)

    shell_root = shell_workspace_root_for_run_root(str(project_root))

    assert shell_root == str(project_root / SHELL_WORKSPACE_NAME).replace("\\", "/")


def test_shell_workspace_root_uses_parent_pyruns_root_when_given_script_workspace(tmp_path):
    script_root = tmp_path / DEFAULT_ROOT_NAME / "main"
    script_root.mkdir(parents=True)

    shell_root = shell_workspace_root_for_run_root(str(script_root))

    assert shell_root == str(tmp_path / DEFAULT_ROOT_NAME / SHELL_WORKSPACE_NAME).replace("\\", "/")


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


def test_bootstrap_shell_workspace_records_project_root(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    shell_root = bootstrap_shell_workspace(str(project_root / DEFAULT_ROOT_NAME))
    info = json.loads(Path(shell_root, SCRIPT_INFO_FILENAME).read_text(encoding="utf-8"))

    assert shell_root == str(project_root / DEFAULT_ROOT_NAME / SHELL_WORKSPACE_NAME).replace("\\", "/")
    assert info["project_root"] == str(project_root).replace("\\", "/")


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
def test_build_command_hydra_requires_shell_workspace(mock_detect):
    mock_detect.return_value = ("hydra", None)
    with pytest.raises(RuntimeError, match="shell workspace/task"):
        _build_command(None, "train.py", None, {})


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
def test_build_command_unknown_requires_shell_workspace(mock_detect):
    mock_detect.return_value = ("unknown", None)
    with pytest.raises(RuntimeError, match="Unable to detect script config mode safely"):
        _build_command(None, "train.py", None, {})


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_success(mock_popen, mock_emit, mock_detect, tmp_path):
    mock_detect.return_value = ("pyruns_load", None)
    task_dir = str(tmp_path)
    os.makedirs(os.path.join(task_dir, "run_logs"), exist_ok=True)
    
    task_info = {
        "name": "TestTask",
        "script": "script.py",
        "status": "queued",
        "start_times": [],
        "finish_times": [],
    }
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "w") as f:
        json.dump(task_info, f)
        
    # Mock subprocess with PIPE-style stdout
    mock_proc = MagicMock()
    mock_proc.pid = 9999
    mock_proc.wait.return_value = 0  # Success
    # stdout.read1 returns one chunk then empty bytes (EOF)
    mock_proc.stdout.read1 = MagicMock(side_effect=[b"hello output\n", b''])
    mock_popen.return_value = mock_proc
    
    res = run_task_worker(
        task_dir=task_dir,
        name="TestTask",
        created_at="now",
        config={},
        run_index=1
    )
    
    assert res["status"] == "completed"
    assert res["progress"] == 1.0
    
    # Check task_info updated
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "r") as f:
        info = json.load(f)
        
    assert info["status"] == "completed"
    assert info["progress"] == 1.0
    assert len(info["start_times"]) == 1
    assert len(info["finish_times"]) == 1
    assert info["pids"] == [9999]
    assert len(info.get("records", [])) == 1

    # Check log file was written by _tee_output
    log_path = os.path.join(task_dir, "run_logs", "run1.log")
    assert os.path.exists(log_path)
    with open(log_path, "rb") as f:
        assert b"hello output" in f.read()

    # Check emit was called
    assert mock_emit.called


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_failure(mock_popen, mock_emit, mock_detect, tmp_path):
    mock_detect.return_value = ("pyruns_load", None)
    task_dir = str(tmp_path)
    os.makedirs(os.path.join(task_dir, "run_logs"), exist_ok=True)
    
    task_info = {
        "name": "FailTask",
        "script": "script.py",
        "status": "queued",
    }
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "w") as f:
        json.dump(task_info, f)
        
    mock_proc = MagicMock()
    mock_proc.pid = 8888
    mock_proc.wait.return_value = 1  # Failed exit code
    mock_proc.returncode = 1
    # stdout.read1 returns log content then EOF
    mock_proc.stdout.read1 = MagicMock(side_effect=[b"Some log output", b''])
    mock_popen.return_value = mock_proc
    
    res = run_task_worker(
        task_dir=task_dir,
        name="FailTask",
        created_at="now",
        config={},
        run_index=1
    )
    
    assert res["status"] == "failed"
    assert res["progress"] == 0.0
    
    # Check task_info updated
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "r") as f:
        info = json.load(f)
    assert info["status"] == "failed"
    
    # Check failed run keeps run1.log and appends a failure summary to error.log
    run_log = os.path.join(task_dir, "run_logs", "run1.log")
    assert os.path.exists(run_log)
    with open(run_log, "r", encoding="utf-8", errors="replace") as f:
        assert "Some log output" in f.read()
    error_log = os.path.join(task_dir, "run_logs", "error.log")
    assert os.path.exists(error_log)
    with open(error_log, "r", encoding="utf-8") as f:
        content = f.read()
        assert "Run #1 failed" in content
        assert "reason=exit_code 1" in content


def test_task_manager_start_batch_tasks_uses_available_slots_immediately(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    tasks = [generator.create_task(f"task-{idx}", {"value": idx}) for idx in range(5)]

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    submitted: list[tuple[str, int, bool]] = []

    def fake_submit(target, run_index, *, independent, execution_mode=None):
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


def test_task_manager_start_task_now_skips_active_task(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    task = generator.create_task("runner", {"value": 1})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    with manager._lock:
        active = manager._tasks_by_name[task["name"]]
        active["status"] = "running"
        active["run_index"] = 1
        manager._running_ids.add(task["name"])
        manager._recompute_processing_flag_locked()

    submitted: list[str] = []
    monkeypatch.setattr(
        manager,
        "_submit_task",
        lambda target, run_index, *, independent, execution_mode=None: submitted.append(target["name"]),
    )

    manager.start_task_now(task["name"])

    assert submitted == []
    refreshed = manager.get_task(task["name"])
    assert refreshed["status"] == "running"
    assert refreshed["run_index"] == 1


def test_task_manager_start_batch_tasks_skips_active_tasks(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    tasks = [generator.create_task(f"task-{idx}", {"value": idx}) for idx in range(3)]

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    with manager._lock:
        active = manager._tasks_by_name[tasks[0]["name"]]
        active["status"] = "running"
        active["run_index"] = 1
        manager._running_ids.add(active["name"])
        manager._recompute_processing_flag_locked()

    submitted: list[tuple[str, int, bool]] = []

    def fake_submit(target, run_index, *, independent, execution_mode=None):
        submitted.append((target["name"], run_index, independent))

    monkeypatch.setattr(manager, "_submit_task", fake_submit)

    manager.start_batch_tasks([task["name"] for task in tasks], max_workers=2)

    assert [item[0] for item in submitted] == [tasks[1]["name"]]
    statuses = {task["name"]: task["status"] for task in manager.list_tasks()}
    assert statuses[tasks[0]["name"]] == "running"
    assert statuses[tasks[1]["name"]] == "running"
    assert statuses[tasks[2]["name"]] == "queued"
    assert manager.get_task(tasks[0]["name"])["run_index"] == 1


def test_task_manager_cancel_task_writes_cancel_reason(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "runner"
    task_dir.mkdir()
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda pid: True)
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

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    monkeypatch.setattr(manager, "_latest_pid_from_disk", lambda task: 12345)
    monkeypatch.setattr("pyruns.core.task_manager.kill_process", lambda pid: None)

    assert manager.cancel_task("runner") is True

    info = json.loads((task_dir / TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    assert info["status"] == "failed"
    assert info["_pending_stop_summary"]["reason"] == "cancelled_by_user"
    assert info["_pending_stop_summary"]["detail_lines"] == ["previous_status=running"]


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_merges_pending_stop_summary_into_single_error_block(mock_popen, mock_emit, mock_detect, tmp_path):
    mock_detect.return_value = ("pyruns_load", None)
    task_dir = str(tmp_path)
    os.makedirs(os.path.join(task_dir, "run_logs"), exist_ok=True)

    task_info = {
        "name": "StopTask",
        "script": "script.py",
        "status": "failed",
        "run_index": 1,
        "start_times": ["2026-03-20_00-00-01"],
        "finish_times": [""],
        "pids": [7777],
        "_pending_stop_summary": {
            "run_index": 1,
            "event": "stopped",
            "reason": "cancelled_by_user",
            "detail_lines": ["previous_status=running"],
        },
    }
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "w", encoding="utf-8") as f:
        json.dump(task_info, f)

    mock_proc = MagicMock()
    mock_proc.pid = 7777
    mock_proc.wait.return_value = 1
    mock_proc.returncode = 1
    mock_proc.stdout.read1 = MagicMock(side_effect=[b"stopped output", b""])
    mock_popen.return_value = mock_proc

    res = run_task_worker(
        task_dir=task_dir,
        name="StopTask",
        created_at="now",
        config={},
        run_index=1,
    )

    assert res["status"] == "failed"
    error_log = os.path.join(task_dir, "run_logs", "error.log")
    with open(error_log, "r", encoding="utf-8") as f:
        content = f.read()
    assert "Run #1 stopped" in content
    assert "reason=cancelled_by_user" in content
    assert "previous_status=running" in content
    assert "exit_code=1" in content
    assert "reason=exit_code 1" not in content

    final_info = json.loads(Path(task_dir, TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    assert "_pending_stop_summary" not in final_info

# ═══════════════════════════════════════════════════════════════
#  LogEmitter — publish-subscribe event bus
# ═══════════════════════════════════════════════════════════════

from pyruns.utils.events import LogEmitter


def test_log_emitter_subscribe_emit():
    emitter = LogEmitter()
    received = []
    emitter.subscribe("task1", lambda chunk: received.append(chunk))
    emitter.emit("task1", "hello\r\n")
    emitter.emit("task1", "world\r\n")
    assert received == ["hello\r\n", "world\r\n"]


def test_log_emitter_unsubscribe():
    emitter = LogEmitter()
    received = []
    cb = lambda chunk: received.append(chunk)
    emitter.subscribe("task1", cb)
    emitter.emit("task1", "before")
    emitter.unsubscribe("task1", cb)
    emitter.emit("task1", "after")
    assert received == ["before"]


def test_log_emitter_multiple_subscribers():
    emitter = LogEmitter()
    r1, r2 = [], []
    emitter.subscribe("task1", lambda c: r1.append(c))
    emitter.subscribe("task1", lambda c: r2.append(c))
    emitter.emit("task1", "data")
    assert r1 == ["data"]
    assert r2 == ["data"]


def test_log_emitter_no_subscribers():
    """emit with no subscribers should not raise."""
    emitter = LogEmitter()
    emitter.emit("nonexistent_task", "should not crash")


def test_log_emitter_isolation():
    """Subscribers only receive events for their subscribed task."""
    emitter = LogEmitter()
    received = []
    emitter.subscribe("task_A", lambda c: received.append(c))
    emitter.emit("task_B", "wrong task")
    emitter.emit("task_A", "right task")
    assert received == ["right task"]


# ═══════════════════════════════════════════════════════════════
#  TaskGenerator — task creation and file writing
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
#  create_task_object
# ═══════════════════════════════════════════════════════════════

class TestCreateTaskObject:
    def test_basic_fields(self):
        obj = create_task_object("/tmp/task1", "my-task", config={"lr": 0.01})
        assert obj["dir"] == "/tmp/task1"
        assert obj["name"] == "my-task"
        assert obj["status"] == "pending"
        assert obj["config"] == {"lr": 0.01}
        assert obj["env"] == {}

    def test_created_at_format(self):
        obj = create_task_object("/tmp", "t", config={})
        # Should be like "2026-02-12 15:30:00"
        assert len(obj["created_at"]) == 19
        assert "-" in obj["created_at"]
        assert "_" in obj["created_at"]  # 2026-02-12_15-30-00

    def test_shell_task_fields(self):
        obj = create_task_object(
            "/tmp/task-shell",
            "shell-task",
            task_kind=TASK_KIND_SHELL,
            config_text="echo hello\n",
        )
        assert obj["config_mode"] == TASK_KIND_SHELL
        assert obj["task_kind"] == TASK_KIND_SHELL
        assert obj["config_file"] == SHELL_CONFIG_FILENAME
        assert obj["config_text"] == "echo hello\n"


# ═══════════════════════════════════════════════════════════════
#  TaskGenerator.create_task
# ═══════════════════════════════════════════════════════════════

class TestTaskGeneratorCreateTask:
    def test_creates_folder(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        cfg = {"lr": 0.01, "epochs": 10}
        task = gen.create_task("my-exp", cfg)

        assert os.path.isdir(task["dir"])

    def test_folder_name_matches_prefix(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("baseline", {"lr": 0.01})

        folder = os.path.basename(task["dir"])
        assert folder.startswith("baseline")

    def test_writes_task_info_json(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("exp1", {"lr": 0.01})

        info_path = os.path.join(task["dir"], "task_info.json")
        assert os.path.exists(info_path)
        with open(info_path, "r", encoding="utf-8") as f:
            info = json.load(f)
        assert info["name"] == "exp1"
        assert info["status"] == "pending"

    def test_writes_config_yaml(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        cfg = {"lr": 0.01, "model": {"name": "resnet"}}
        task = gen.create_task("exp2", cfg)

        cfg_path = os.path.join(task["dir"], "config.yaml")
        assert os.path.exists(cfg_path)
        with open(cfg_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        assert loaded["lr"] == 0.01
        assert loaded["model"]["name"] == "resnet"

    def test_creates_run_logs_dir(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("exp3", {"x": 1})

        log_dir = os.path.join(task["dir"], "run_logs")
        assert os.path.isdir(log_dir)
        # Log file is NOT created until execution starts
        assert not os.path.exists(os.path.join(log_dir, "run1.log"))

    def test_meta_keys_stripped_from_config(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        cfg = {"lr": 0.01, "_meta_desc": "lr=0.01", "_meta_other": "x"}
        task = gen.create_task("exp4", cfg)

        cfg_path = os.path.join(task["dir"], "config.yaml")
        with open(cfg_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        assert "_meta_desc" not in loaded
        assert "_meta_other" not in loaded
        assert loaded["lr"] == 0.01

    def test_group_index_in_folder_name(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("batch-run", {"x": 1}, group_index="[3-of-10]")

        folder = os.path.basename(task["dir"])
        assert "batch-run_[3-of-10]" in folder

    def test_display_name_with_group_index(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("batch-run", {"x": 1}, group_index="[3-of-10]")
        assert task["name"] == "batch-run_[3-of-10]"

    def test_deduplication_on_name_clash(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        t1 = gen.create_task("same-name", {"x": 1})
        t2 = gen.create_task("same-name", {"x": 2})

        assert t1["dir"] != t2["dir"]
        assert os.path.isdir(t1["dir"])
        assert os.path.isdir(t2["dir"])
        assert t2["name"] == os.path.basename(t2["dir"])
        with open(os.path.join(t2["dir"], "task_info.json"), "r", encoding="utf-8") as f:
            info = json.load(f)
        assert info["name"] == t2["name"]

    def test_empty_prefix_uses_timestamp(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("", {"x": 1})

        folder = os.path.basename(task["dir"])
        assert folder.startswith("task_")

    def test_task_kind_written_per_task(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task_cfg = gen.create_task("cfg-task", {"x": 1}, task_kind=TASK_KIND_CONFIG)
        with patch("pyruns.core.task_generator.get_shell_config_filename_for_workspace", return_value=SHELL_CONFIG_FILENAME):
            task_shell = gen.create_shell_task("shell-task", "echo shell\n")

        with open(os.path.join(task_cfg["dir"], "task_info.json"), "r", encoding="utf-8") as f:
            info_cfg = json.load(f)
        with open(os.path.join(task_shell["dir"], "task_info.json"), "r", encoding="utf-8") as f:
            info_shell = json.load(f)

        assert info_cfg["task_kind"] == TASK_KIND_CONFIG
        assert info_cfg["config_mode"] == TASK_KIND_CONFIG
        assert info_cfg["config_file"] == CONFIG_FILENAME
        assert info_shell["task_kind"] == TASK_KIND_SHELL
        assert info_shell["config_mode"] == TASK_KIND_SHELL
        assert info_shell["config_file"] == SHELL_CONFIG_FILENAME

    def test_create_shell_task_uses_runtime_specific_payload_filename(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))

        with patch(
            "pyruns.core.task_generator.get_shell_config_filename_for_workspace",
            return_value=POWERSHELL_CONFIG_FILENAME,
        ):
            task_shell = gen.create_shell_task("shell-task", "Write-Host 'hello'\n")

        assert task_shell["config_mode"] == TASK_KIND_SHELL
        assert task_shell["config_file"] == POWERSHELL_CONFIG_FILENAME
        assert os.path.exists(os.path.join(task_shell["dir"], POWERSHELL_CONFIG_FILENAME))

    def test_invalid_task_kind_is_rejected(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        with pytest.raises(ValueError, match="Unsupported task kind"):
            gen.create_task("invalid", {"x": 1}, task_kind="unknown-kind")

    def test_invalid_task_name_is_rejected(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        with pytest.raises(ValueError, match="invalid characters"):
            gen.create_task("bad/name", {"x": 1})


# ═══════════════════════════════════════════════════════════════
#  TaskGenerator.create_tasks (batch)
# ═══════════════════════════════════════════════════════════════

class TestTaskGeneratorCreateTasks:
    def test_single_config(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        tasks = gen.create_tasks([{"x": 1}], "single")
        assert len(tasks) == 1
        # No index suffix for single task
        assert tasks[0]["name"] == "single"

    def test_multiple_configs_get_index(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        configs = [{"x": i} for i in range(3)]
        tasks = gen.create_tasks(configs, "batch")

        assert len(tasks) == 3
        assert tasks[0]["name"] == "batch_[1-of-3]"
        assert tasks[1]["name"] == "batch_[2-of-3]"
        assert tasks[2]["name"] == "batch_[3-of-3]"

    def test_batch_with_pipe_configs(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        base = {"lr": "0.001 | 0.01", "bs": 32}
        configs = generate_batch_configs(base)
        assert len(configs) == 2

        tasks = gen.create_tasks(configs, "exp")
        assert len(tasks) == 2
        # Configs should have typed values, not pipe strings
        for task in tasks:
            cfg_path = os.path.join(task["dir"], "config.yaml")
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            assert isinstance(cfg["lr"], (int, float))

    def test_batch_folders_unique(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        configs = [{"x": i} for i in range(5)]
        tasks = gen.create_tasks(configs, "run")

        dirs = [t["dir"] for t in tasks]
        assert len(set(dirs)) == 5  # all unique


# ═══════════════════════════════════════════════════════════════
#  Report — CSV and JSON export
# ═══════════════════════════════════════════════════════════════


def _make_task(tmp_path, name, records=None, starts=None, finishes=None, pids=None):
    """Create a task dict with a real task_info.json on disk."""
    task_dir = str(tmp_path / name)
    os.makedirs(task_dir, exist_ok=True)
    info = {
        "name": name,
        "status": "completed",
        "start_times": starts or ["2026-01-01 00:00:00"],
        "finish_times": finishes or ["2026-01-01 00:01:00"],
        "pids": pids or [12345],
    }
    if records is not None:
        info[RECORDS_KEY] = records
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "w") as f:
        json.dump(info, f)
    return {
        "name": name,
        "status": "completed",
        "dir": task_dir,
        "start_times": info["start_times"],
        "finish_times": info["finish_times"],
        "pids": info["pids"],
    }


class TestBuildExportCSV:
    def test_single_task_single_run(self, tmp_path):
        task = _make_task(tmp_path, "t1", records=[{"loss": 0.5, "acc": 92}])
        csv_str = build_export_csv([task])
        reader = csv.DictReader(io.StringIO(csv_str))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["name"] == "t1"
        assert rows[0]["run"] == "1"
        assert rows[0]["loss"] == "0.5"
        assert rows[0]["acc"] == "92"

    def test_multi_run(self, tmp_path):
        task = _make_task(
            tmp_path, "t2",
            records=[{"loss": 0.5}, {"loss": 0.1}],
            starts=["2026-01-01 00:00:00", "2026-01-02 00:00:00"],
            finishes=["2026-01-01 00:01:00", "2026-01-02 00:01:00"],
            pids=[111, 222],
        )
        csv_str = build_export_csv([task])
        reader = csv.DictReader(io.StringIO(csv_str))
        rows = list(reader)
        assert len(rows) == 2
        assert rows[0]["run"] == "1"
        assert rows[1]["run"] == "2"
        assert rows[0]["pid"] == "111"
        assert rows[1]["pid"] == "222"

    def test_empty_tasks(self):
        csv_str = build_export_csv([])
        assert csv_str == ""

    def test_column_order(self, tmp_path):
        task = _make_task(tmp_path, "t3", records=[{"zeta": 1, "alpha": 2}])
        csv_str = build_export_csv([task])
        reader = csv.DictReader(io.StringIO(csv_str))
        cols = reader.fieldnames
        # Priority columns should come first
        assert cols[:4] == ["name", "status", "run", "start_time"]


class TestBuildExportJSON:
    def test_basic(self, tmp_path):
        task = _make_task(tmp_path, "j1", records=[{"loss": 0.3}])
        result = json.loads(build_export_json([task]))
        assert len(result) == 1
        assert result[0]["task_name"] == "j1"
        assert result[0]["monitor"] == [{"loss": 0.3}]

    def test_no_monitor_excluded(self, tmp_path):
        task = _make_task(tmp_path, "j2")
        result = json.loads(build_export_json([task]))
        assert len(result) == 0  # tasks with no monitor are excluded


