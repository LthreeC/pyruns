"""Regression tests for task lifecycle state normalization."""

import time
import socket

import pytest

from pyruns.core.task_generator import TaskGenerator
from pyruns.core.task_manager import TaskManager
from pyruns.utils.runner_device import local_runner_device
from pyruns.utils.info_io import load_task_metadata, update_task_info


@pytest.mark.parametrize("raw_status", ["RUNNING", " RUNNING "])
def test_running_status_is_canonical_for_counts_filters_and_reads(tmp_path, raw_status):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("status-case", {"value": 1})

    update_task_info(
        task["dir"],
        lambda info: info.update(
            {
                "status": raw_status,
                "run_index": 1,
                "start_times": ["2026-10-30_00-00-00"],
                "finish_times": [""],
                "pids": [987654321],
                "runner_id": "other-host:123:live",
                "runner_host": "other-host",
                "lease_until": time.time() + 60,
            }
        ),
    )

    assert load_task_metadata(task["dir"])["status"] == "running"

    manager = TaskManager(
        tasks_dir=str(tasks_dir),
        lazy_scan=False,
        owns_task_lifecycle=False,
        reconcile_unowned_running=True,
    )
    try:
        assert manager.get_task("status-case")["status"] == "running"
        assert manager.active_task_count() == 1
        page, total, counts = manager.get_task_page(status=" running ")
        assert [item["name"] for item in page] == ["status-case"]
        assert total == 1
        assert counts["running"] == 1
    finally:
        manager.shutdown()


def test_whitespace_running_status_is_reconciled_when_local_process_is_gone(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("stale-status", {"value": 1})

    update_task_info(
        task["dir"],
        lambda info: info.update(
            {
                "status": " RUNNING ",
                "run_index": 1,
                "start_times": ["2026-10-30_00-00-00"],
                "finish_times": [""],
                "pids": [987654321],
                "runner_id": "stale-runner",
                "runner_host": socket.gethostname().lower(),
                "runner_device": local_runner_device(),
                "lease_until": time.time() - 60,
            }
        ),
    )
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda _pid: False)

    manager = TaskManager(
        tasks_dir=str(tasks_dir),
        lazy_scan=False,
        owns_task_lifecycle=False,
        reconcile_unowned_running=True,
    )
    try:
        assert manager.get_task("stale-status")["status"] == "failed"
        assert load_task_metadata(task["dir"])["status"] == "failed"
    finally:
        manager.shutdown()
