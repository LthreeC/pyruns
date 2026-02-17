"""
Tests for pyruns.utils.task_io — task_info I/O, monitor data, log options.
"""
import os
import json
import pytest

from pyruns._config import INFO_FILENAME, RUN_LOG_DIR, MONITOR_KEY
from pyruns.utils.task_io import (
    load_task_info,
    save_task_info,
    load_monitor_data,
    get_log_options,
    resolve_log_path,
)


class TestLoadSaveTaskInfo:
    def test_roundtrip(self, tmp_path):
        task_dir = str(tmp_path)
        info = {"name": "test", "status": "pending", "extra": [1, 2, 3]}
        save_task_info(task_dir, info)
        loaded = load_task_info(task_dir)
        assert loaded == info

    def test_load_missing_file(self, tmp_path):
        assert load_task_info(str(tmp_path)) == {}

    def test_load_corrupt_json(self, tmp_path):
        path = os.path.join(str(tmp_path), INFO_FILENAME)
        with open(path, "w") as f:
            f.write("{invalid json")
        assert load_task_info(str(tmp_path)) == {}

    def test_load_corrupt_raises_when_requested(self, tmp_path):
        path = os.path.join(str(tmp_path), INFO_FILENAME)
        with open(path, "w") as f:
            f.write("{invalid json")
        with pytest.raises(json.JSONDecodeError):
            load_task_info(str(tmp_path), raise_error=True)

    def test_unicode_support(self, tmp_path):
        task_dir = str(tmp_path)
        info = {"name": "测试任务", "description": "中文描述 🧪"}
        save_task_info(task_dir, info)
        loaded = load_task_info(task_dir)
        assert loaded == info


class TestLoadMonitorData:
    def test_with_monitors(self, tmp_path):
        task_dir = str(tmp_path)
        info = {MONITOR_KEY: [{"loss": 0.5}, {"loss": 0.1}]}
        save_task_info(task_dir, info)
        data = load_monitor_data(task_dir)
        assert len(data) == 2
        assert data[0]["loss"] == 0.5

    def test_without_monitors(self, tmp_path):
        task_dir = str(tmp_path)
        save_task_info(task_dir, {"name": "test"})
        assert load_monitor_data(task_dir) == []

    def test_missing_file(self, tmp_path):
        assert load_monitor_data(str(tmp_path)) == []


class TestGetLogOptions:
    def test_run_logs(self, tmp_path):
        task_dir = str(tmp_path)
        log_dir = os.path.join(task_dir, RUN_LOG_DIR)
        os.makedirs(log_dir)
        for name in ["run1.log", "run2.log", "run10.log"]:
            open(os.path.join(log_dir, name), "w").close()

        opts = get_log_options(task_dir)
        keys = list(opts.keys())
        assert keys == ["run1.log", "run2.log", "run10.log"]
        assert all(os.path.isfile(p) for p in opts.values())

    def test_no_logs(self, tmp_path):
        assert get_log_options(str(tmp_path)) == {}


class TestResolveLogPath:
    def test_resolve_named(self, tmp_path):
        task_dir = str(tmp_path)
        log_dir = os.path.join(task_dir, RUN_LOG_DIR)
        os.makedirs(log_dir)
        path = os.path.join(log_dir, "run1.log")
        open(path, "w").close()

        result = resolve_log_path(task_dir, "run1.log")
        assert result == path

    def test_resolve_latest(self, tmp_path):
        task_dir = str(tmp_path)
        log_dir = os.path.join(task_dir, RUN_LOG_DIR)
        os.makedirs(log_dir)
        for name in ["run1.log", "run2.log"]:
            open(os.path.join(log_dir, name), "w").close()

        result = resolve_log_path(task_dir)
        assert result.endswith("run2.log")

    def test_resolve_no_logs(self, tmp_path):
        assert resolve_log_path(str(tmp_path)) is None
