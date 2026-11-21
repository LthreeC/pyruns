"""Durable worker results that can be replayed by any shared-workspace reader."""

from __future__ import annotations

import copy
import hashlib
import os
from typing import Any, Callable

from pyruns.utils.info_io import (
    _load_json_object,
    _write_task_info_unlocked,
    ensure_run_slot,
    run_slot_count,
    task_attempt_matches,
    update_task_metadata,
    validate_task_directory,
    validate_workspace_file,
)


def _result_path(task_dir: str, run_index: int, runner_id: str, attempt_id: str) -> str:
    identity = "\0".join((str(run_index), runner_id, attempt_id))
    token = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return os.path.join(task_dir, f".run-result-{token}.json")


def write_run_result(task_dir: str, result: dict[str, Any]) -> None:
    """Save the observed exit before taking the contended task metadata lock."""
    validate_task_directory(task_dir)
    path = _result_path(task_dir, result["run_index"], result["runner_id"], result["attempt_id"])
    validate_workspace_file(path, task_dir, label="Run result")
    _write_task_info_unlocked(path, task_dir, result)


def recover_run_result(
    task_dir: str, info: dict[str, Any], *, timeout_sec: float = 1.0, read_only: bool | Callable[[], bool] = False,
) -> tuple[dict[str, Any], bool]:
    """Replay only the current running attempt, checking identity under its lock."""
    if info.get("status") != "running":
        return info, False
    run_index = run_slot_count(info)
    runner_id = str(info.get("runner_id", "") or "")
    attempt_id = str(info.get("_attempt_id", "") or "")
    path = _result_path(task_dir, run_index, runner_id, attempt_id)
    validate_task_directory(task_dir)
    validate_workspace_file(path, task_dir, label="Run result")
    try:
        result = _load_json_object(
            path, max_bytes=16 * 1024, label="Run result", _boundary=os.path.realpath(task_dir),
        )
    except FileNotFoundError:
        return info, False
    if (
        result.get("run_index") != run_index or result.get("runner_id") != runner_id
        or result.get("attempt_id") != attempt_id
        or result.get("status") not in {"completed", "failed", "cancelled"}
        or not isinstance(result.get("finish_time"), str) or not result["finish_time"]
    ):
        raise ValueError(f"Invalid run result: {path}")

    recovered = False

    def apply(current: dict[str, Any]) -> None:
        nonlocal recovered
        if (
            current.get("status") != "running" or run_slot_count(current) != run_index
            or str(current.get("runner_id", "") or "") != runner_id
            or not task_attempt_matches(current, attempt_id)
        ):
            return
        slot = ensure_run_slot(current, run_index)
        status = result["status"]
        summary = current.get("_pending_stop_summary")
        if current.get("cancel_requested_at") or (
            isinstance(summary, dict) and summary.get("run_index") == run_index
        ):
            status = "cancelled"
        current["status"] = status
        current["progress"] = 1.0 if status == "completed" else 0.0
        current["run_statuses"][slot] = status
        current["finish_times"][slot] = result["finish_time"]
        if not current["start_times"][slot]:
            current["start_times"][slot] = result.get("start_time", "")
        for field, key in (
            ("pids", "pid"), ("pid_create_times", "pid_create_time"),
            ("durations", "duration_seconds"), ("exit_codes", "exit_code"),
        ):
            if result.get(key) is not None:
                current[field][slot] = result[key]
        for key in (
            "runner_id", "runner_host", "runner_device", "lease_until", "lease_heartbeat", "_pending_stop_summary",
            "_scheduled_env", "_gpu_assignment", "_gpu_wait_override",
        ):
            current.pop(key, None)
        recovered = True

    snapshot_only = read_only() if callable(read_only) else read_only
    if snapshot_only:
        updated = copy.deepcopy(info)
        apply(updated)
    else:
        updated = update_task_metadata(task_dir, apply, timeout_sec=timeout_sec)
    return updated, recovered
