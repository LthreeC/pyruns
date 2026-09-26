"""Tests for CSV and JSON run-history exports."""
import csv
import io
import json
import os
from pathlib import Path

import pytest

from pyruns._config import RECORDS_KEY, TASK_INFO_FILENAME
from pyruns.core.report import build_export_csv, build_export_json
from pyruns.utils.info_io import load_task_info, save_task_info, update_task_info


def _make_task(tmp_path, name, records=None, starts=None, finishes=None, pids=None, **metadata):
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
    info.update(metadata)
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "w") as f:
        json.dump(info, f)
    return {**info, "dir": task_dir}


class TestBuildExportCSV:
    @pytest.mark.parametrize("exit_literal", ["1e10000", "-1e10000", "NaN"])
    def test_nonfinite_legacy_exit_codes_preserve_csv_and_json_error_handling(self, tmp_path, exit_literal):
        task = _make_task(tmp_path, "invalid-exit")
        info_path = Path(task["dir"]) / TASK_INFO_FILENAME
        info_path.write_text(
            '{"name":"invalid-exit","status":"failed","run_index":1,"exit_codes":[' + exit_literal + ']}'
        )
        task.update(load_task_info(task["dir"], raise_error=True))
        healthy = _make_task(tmp_path, "healthy", exit_codes=[0])

        rows = list(csv.DictReader(io.StringIO(build_export_csv([task, healthy]))))
        assert [(row["name"], row["status"]) for row in rows] == [
            ("invalid-exit", "failed"), ("healthy", "completed"),
        ]
        with pytest.raises(ValueError, match="strict JSON"):
            build_export_json([task, healthy])

    def test_single_task_single_run(self, tmp_path):
        task = _make_task(tmp_path, "t1", records=[{"loss": 0.5, "acc": 92}], durations=[12.345], exit_codes=[0])
        csv_str = build_export_csv([task])
        reader = csv.DictReader(io.StringIO(csv_str))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["name"] == "t1"
        assert rows[0]["run"] == "1"
        assert rows[0]["loss"] == "0.5"
        assert rows[0]["acc"] == "92"
        assert rows[0]["duration_seconds"] == "12.345"
        assert rows[0]["exit_code"] == "0"

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

    def test_uses_platform_neutral_lf_line_endings(self, tmp_path):
        task = _make_task(tmp_path, "t3", records=[{"loss": 0.5}])

        csv_str = build_export_csv([task])

        assert "\r" not in csv_str
        assert csv_str.count("\n") == 2

    def test_column_order(self, tmp_path):
        task = _make_task(tmp_path, "t3", records=[{"zeta": 1, "alpha": 2}])
        csv_str = build_export_csv([task])
        reader = csv.DictReader(io.StringIO(csv_str))
        cols = reader.fieldnames
        # Priority columns should come first
        assert cols[:4] == ["name", "status", "run", "start_time"]

    def test_monitor_fields_cannot_override_lifecycle_columns(self, tmp_path):
        task = _make_task(
            tmp_path,
            "safe-name",
            records=[{"name": "spoofed", "status": "running", "run": 999, "loss": 0.5}],
            exit_codes=[0],
        )

        row = next(csv.DictReader(io.StringIO(build_export_csv([task]))))

        assert row["name"] == "safe-name"
        assert row["status"] == "completed"
        assert row["run"] == "1"
        assert row["loss"] == "0.5"

    def test_formula_like_values_are_neutralized_for_spreadsheets(self, tmp_path):
        task = _make_task(tmp_path, "formula", records=[{"note": "+cmd", "=dangerous-header": "value"}], exit_codes=[0])
        task["name"] = "=HYPERLINK(\"https://example.invalid\")"

        row = next(csv.DictReader(io.StringIO(build_export_csv([task]))))

        assert row["name"].startswith("'=")
        assert row["note"] == "'+cmd"
        assert "'=dangerous-header" in row


class TestBuildExportJSON:
    def test_basic(self, tmp_path):
        task = _make_task(tmp_path, "j1", records=[{"loss": 0.3}])
        result = json.loads(build_export_json([task]))
        assert len(result) == 1
        assert result[0]["name"] == "j1"
        assert result[0]["run"] == 1
        assert result[0]["loss"] == 0.3

    def test_output_remains_ascii_safe_for_supplementary_unicode(self, tmp_path):
        task = _make_task(tmp_path, "emoji-😀", records=[{"label": "😀"}])

        document = build_export_json([task])

        assert document.isascii()
        assert json.loads(document)[0]["label"] == "😀"

    def test_no_monitor_still_exports_run_history(self, tmp_path):
        task = _make_task(tmp_path, "j2")
        result = json.loads(build_export_json([task]))
        assert len(result) == 1
        assert result[0]["name"] == "j2"
        assert result[0]["run"] == 1

    def test_never_run_task_does_not_fabricate_run_one(self, tmp_path):
        task_dir = tmp_path / "never-run"
        task_dir.mkdir()
        save_task_info(str(task_dir), {"name": "never-run", "status": "pending"})
        task = {"name": "never-run", "status": "pending", "dir": str(task_dir)}

        assert json.loads(build_export_json([task])) == []

    def test_historical_status_is_derived_per_run(self, tmp_path):
        task = _make_task(
            tmp_path,
            "rerun",
            records=[{"loss": 0.5}], starts=["first"], finishes=["first-done"], pids=[111],
            status="cancelled", exit_codes=[7], run_statuses=["cancelled"], durations=[1.0],
        )
        # A rerun may update disk after task selection, before report generation.
        update_task_info(
            task["dir"],
            lambda info: info.update(
                status="completed", run_index=2, records=[{"loss": 0.5}, {"loss": 0.1}],
                start_times=["first", "second"], finish_times=["first-done", "second-done"],
                pids=[111, 222], exit_codes=[7, 0], run_statuses=["cancelled", "completed"], durations=[1.0, 2.0],
            ),
        )

        rows = json.loads(build_export_json([task]))

        assert rows == [
            {"name": "rerun", "status": "cancelled", "run": 1, "start_time": "first", "finish_time": "first-done",
             "duration_seconds": 1.0, "exit_code": 7, "pid": 111, "loss": 0.5},
            {"name": "rerun", "status": "completed", "run": 2, "start_time": "second", "finish_time": "second-done",
             "duration_seconds": 2.0, "exit_code": 0, "pid": 222, "loss": 0.1},
        ]
        completed_only = json.loads(build_export_json([task], statuses={"completed"}))
        assert [row["run"] for row in completed_only] == [2]

    def test_rejects_non_finite_json_metrics(self, tmp_path):
        task = _make_task(tmp_path, "nan", records=[{"loss": float("nan")}], exit_codes=[0])

        with pytest.raises(ValueError, match="JSON"):
            build_export_json([task])
