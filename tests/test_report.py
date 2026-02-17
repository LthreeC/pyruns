"""
Tests for pyruns.core.report — CSV and JSON export builders.
"""
import csv
import io
import json
import os
import pytest

from pyruns._config import INFO_FILENAME, MONITOR_KEY
from pyruns.core.report import build_export_csv, build_export_json


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
        "id": name,
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
        assert cols[:4] == ["name", "id", "status", "run"]


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
