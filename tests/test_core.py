"""
Tests for pyruns.core — config_manager, system_metrics, executor,
task_generator, and report.
"""
import csv
import io
import json
import os
import sys

import pytest
import yaml
from unittest.mock import patch, MagicMock

from pyruns._config import (
    ENV_CONFIG, CONFIG_FILENAME, INFO_FILENAME, MONITOR_KEY,
)
from pyruns.core.config_manager import ConfigNode, ConfigManager
from pyruns.core.executor import _prepare_env, _build_command, run_task_worker
from pyruns.core.report import build_export_csv, build_export_json
from pyruns.core.system_metrics import SystemMonitor
from pyruns.core.task_generator import TaskGenerator, create_task_object
from pyruns.utils.batch_utils import generate_batch_configs

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
    
    # Setup GPU mock
    mock_subprocess.return_value = b"0, 45.0, 4000.0, 8000.0\n1, 90.0, 8000.0, 8000.0\n"
    
    monitor = SystemMonitor()
    metrics = monitor.sample()
    
    # Assert CPU/RAM
    assert metrics["cpu_percent"] == 25.5
    assert metrics["mem_percent"] == 60.0
    
    # Assert GPU
    gpus = metrics["gpus"]
    assert len(gpus) == 2
    assert gpus[0]["index"] == 0
    assert gpus[0]["util"] == 45.0
    assert gpus[0]["mem_used"] == 4000.0
    assert gpus[0]["mem_total"] == 8000.0
    
    assert gpus[1]["index"] == 1
    assert gpus[1]["util"] == 90.0
    
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
    mock_subprocess.return_value = b"   \n\n\n"
    
    monitor = SystemMonitor()
    gpus = monitor._get_gpu_metrics()
    assert gpus == []


# ═══════════════════════════════════════════════════════════════
#  Executor
# ═══════════════════════════════════════════════════════════════


def test_prepare_env():
    env1 = _prepare_env(extra_env={"CUDA_VISIBLE_DEVICES": "1"})
    assert env1["PYTHONIOENCODING"] == "utf-8"
    assert env1["CUDA_VISIBLE_DEVICES"] == "1"
    
    env2 = _prepare_env(task_dir="/fake/dir")
    assert env2[ENV_CONFIG] == os.path.join("/fake/dir", CONFIG_FILENAME)


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
    
    cmd, wd = _build_command(None, script_path, None, config)
    
    # sys.executable, train.py, --lr, 0.05, --epochs, 10, --flag
    assert cmd[0] == sys.executable
    assert cmd[1] == "train.py"
    assert "--lr" in cmd
    assert "0.05" in cmd
    assert "--flag" in cmd
    

@patch("pyruns.utils.parse_utils.detect_config_source_fast")
def test_build_command_non_argparse(mock_detect):
    mock_detect.return_value = ("pyruns_read", None)
    
    script_path = "train.py"
    config = {"lr": 0.05}
    
    cmd, wd = _build_command(None, script_path, None, config)
    
    # Should only contain python and script, no args appended
    assert len(cmd) == 2
    assert cmd[0] == sys.executable
    assert cmd[1] == "train.py"


@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_success(mock_popen, mock_emit, tmp_path):
    task_dir = str(tmp_path)
    os.makedirs(os.path.join(task_dir, "run_logs"), exist_ok=True)
    
    task_info = {
        "name": "TestTask",
        "script": "script.py",
        "status": "queued",
        "start_times": [],
        "finish_times": [],
    }
    with open(os.path.join(task_dir, INFO_FILENAME), "w") as f:
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
    with open(os.path.join(task_dir, INFO_FILENAME), "r") as f:
        info = json.load(f)
        
    assert info["status"] == "completed"
    assert info["progress"] == 1.0
    assert len(info["start_times"]) == 1
    assert len(info["finish_times"]) == 1
    assert 9999 in info["pids"]
    assert len(info.get("monitors", [])) == 1

    # Check log file was written by _tee_output
    log_path = os.path.join(task_dir, "run_logs", "run1.log")
    assert os.path.exists(log_path)
    with open(log_path, "rb") as f:
        assert b"hello output" in f.read()

    # Check emit was called
    assert mock_emit.called


@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_failure(mock_popen, mock_emit, tmp_path):
    task_dir = str(tmp_path)
    os.makedirs(os.path.join(task_dir, "run_logs"), exist_ok=True)
    
    task_info = {
        "name": "FailTask",
        "script": "script.py",
        "status": "queued",
    }
    with open(os.path.join(task_dir, INFO_FILENAME), "w") as f:
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
    with open(os.path.join(task_dir, INFO_FILENAME), "r") as f:
        info = json.load(f)
    assert info["status"] == "failed"
    
    # Check log migration: run1.log content moved to error.log
    run_log = os.path.join(task_dir, "run_logs", "run1.log")
    assert not os.path.exists(run_log)  # Should be deleted after moving
    error_log = os.path.join(task_dir, "run_logs", "error.log")
    assert os.path.exists(error_log)
    with open(error_log, "r", encoding="utf-8") as f:
        content = f.read()
        assert "Some log output" in content
        assert "Reason: Exit Code 1" in content

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
        obj = create_task_object("/tmp/task1", "my-task", {"lr": 0.01})
        assert obj["dir"] == "/tmp/task1"
        assert obj["name"] == "my-task"
        assert obj["status"] == "pending"
        assert obj["config"] == {"lr": 0.01}
        assert obj["env"] == {}

    def test_created_at_format(self):
        obj = create_task_object("/tmp", "t", {})
        # Should be like "2026-02-12 15:30:00"
        assert len(obj["created_at"]) == 19
        assert "-" in obj["created_at"]
        assert "_" in obj["created_at"]  # 2026-02-12_15-30-00


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

    def test_empty_prefix_uses_timestamp(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("", {"x": 1})

        folder = os.path.basename(task["dir"])
        assert folder.startswith("task_")


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


def _make_task(tmp_path, name, monitors=None, starts=None, finishes=None, pids=None):
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
    if monitors is not None:
        info[MONITOR_KEY] = monitors
    with open(os.path.join(task_dir, INFO_FILENAME), "w") as f:
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
        task = _make_task(tmp_path, "t1", monitors=[{"loss": 0.5, "acc": 92}])
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
            monitors=[{"loss": 0.5}, {"loss": 0.1}],
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
        task = _make_task(tmp_path, "t3", monitors=[{"zeta": 1, "alpha": 2}])
        csv_str = build_export_csv([task])
        reader = csv.DictReader(io.StringIO(csv_str))
        cols = reader.fieldnames
        # Priority columns should come first
        assert cols[:4] == ["name", "status", "run", "start_time"]


class TestBuildExportJSON:
    def test_basic(self, tmp_path):
        task = _make_task(tmp_path, "j1", monitors=[{"loss": 0.3}])
        result = json.loads(build_export_json([task]))
        assert len(result) == 1
        assert result[0]["task_name"] == "j1"
        assert result[0]["monitor"] == [{"loss": 0.3}]

    def test_no_monitor_excluded(self, tmp_path):
        task = _make_task(tmp_path, "j2")
        result = json.loads(build_export_json([task]))
        assert len(result) == 0  # tasks with no monitor are excluded


