"""Hidden worker process used by one-shot Pyruns CLI run commands."""

from __future__ import annotations

import argparse
import os
import time
from typing import Any

from pyruns._config import ENV_KEY_ROOT, TASKS_DIR
from pyruns.cli.submission_protocol import (
    RUNNER_CLEANUP_TIMEOUT_SEC,
    abort_requested,
    read_submission_payload,
    remove_control_file,
    submission_control_paths,
    submission_payload_path,
    validate_submission_token,
    write_submission_receipt,
)
from pyruns.core.task_manager import TaskManager, active_task_run_index
from pyruns.utils.info_io import load_task_metadata, read_run_attempt_id, task_attempt_matches
from pyruns.utils.settings import ensure_settings_file, load_settings
from pyruns.update_coordination import EnvironmentActivityLease


_FINAL_STATUSES = {"completed", "failed", "cancelled"}
_ACTIVE_STATUSES = {"queued", "running"}
_CLEANUP_TIMEOUT_SEC = RUNNER_CLEANUP_TIMEOUT_SEC
_POLL_INTERVAL_SEC = 0.05
_MAX_POLL_INTERVAL_SEC = 1.0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--submission-token", required=True)
    parser.add_argument("--web-batch", action="store_true")
    return parser.parse_args()


def _submitted_run_status(
    info: dict[str, Any], run_index: int, *, expected_runner_id: str | None = None,
    expected_attempt_id: str | None = None,
) -> str | None:
    """Return only the lifecycle state belonging to the submitted run."""

    observed_attempt = read_run_attempt_id(info, run_index) or str(info.get("_attempt_id", "") or "")
    if expected_attempt_id is not None and observed_attempt != expected_attempt_id:
        return None

    run_statuses = info.get("run_statuses", [])
    if isinstance(run_statuses, (list, tuple)) and run_index <= len(run_statuses):
        recorded = str(run_statuses[run_index - 1] or "").lower()
        if recorded in _FINAL_STATUSES:
            return recorded

    current = str(info.get("status", "") or "").lower()
    if current in _ACTIVE_STATUSES and active_task_run_index(info) == run_index:
        if expected_runner_id is not None and info.get("runner_id") != expected_runner_id:
            return None
        return current
    try:
        current_run_index = int(info.get("run_index", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if current in _FINAL_STATUSES and (
        current_run_index == run_index
        or (expected_attempt_id is not None and current_run_index < run_index)
    ):
        return current
    return None


def _report(
    path: str,
    *,
    token: str,
    names: list[str],
    run_indices: list[int],
    status: str,
    claimed: list[str],
    detail: str = "",
) -> None:
    write_submission_receipt(
        path,
        token=token,
        runner_pid=os.getpid(),
        status=status,
        names=names,
        run_indices=run_indices,
        claimed=claimed,
        detail=detail,
    )


def _claimed_tasks_stopped(
    tm: TaskManager,
    selected: dict[str, dict[str, Any]],
    claimed: list[str],
    expected_runs: dict[str, int],
) -> bool:
    """Request cancellation and prove that every claimed task is inactive."""

    if not claimed:
        return True
    expected_runner_id = str(getattr(tm, "runner_id", "") or "")
    if not expected_runner_id:
        return False
    attempt_id = getattr(tm, "_submission_attempt_id", None)
    requested: set[str] = set()
    deadline = time.monotonic() + max(0.05, _CLEANUP_TIMEOUT_SEC)
    poll_interval = _POLL_INTERVAL_SEC
    previous_unsettled_count: int | None = None
    while True:
        unsettled: list[str] = []
        for name in claimed:
            task = selected.get(name) or {}
            task_dir = str(task.get("dir", "") or "")
            info = load_task_metadata(task_dir) if task_dir else None
            if not info:
                return False
            if not task_attempt_matches(info, attempt_id):
                return False
            status = str(info.get("status", "") or "").lower()
            if status in _FINAL_STATUSES:
                continue
            if status not in _ACTIVE_STATUSES:
                return False
            if str(info.get("runner_id", "") or "") != expected_runner_id:
                return False
            expected_run_index = expected_runs.get(name)
            if (
                type(expected_run_index) is not int
                or active_task_run_index(info) != expected_run_index
            ):
                return False
            unsettled.append(name)
            if name not in requested:
                try:
                    if tm.request_task_cancel(
                        name,
                        expected_runner_id=expected_runner_id,
                        expected_run_index=expected_run_index,
                        expected_attempt_id=attempt_id,
                    ):
                        requested.add(name)
                except Exception:
                    pass

        if not unsettled:
            return True
        if time.monotonic() >= deadline:
            return False
        progressed = (
            previous_unsettled_count is None
            or len(unsettled) != previous_unsettled_count
        )
        previous_unsettled_count = len(unsettled)
        time.sleep(poll_interval)
        poll_interval = (
            _POLL_INTERVAL_SEC
            if progressed
            else min(_MAX_POLL_INTERVAL_SEC, poll_interval * 1.5)
        )


def _stop_and_report(
    tm: TaskManager,
    *,
    selected: dict[str, dict[str, Any]],
    receipt_file: str,
    token: str,
    names: list[str],
    run_indices: list[int],
    claimed: list[str],
    stopped_status: str,
    detail: str,
) -> int:
    _report(
        receipt_file,
        token=token,
        names=names,
        run_indices=run_indices,
        status="stopping",
        claimed=claimed,
        detail=detail,
    )
    expected_runs = dict(zip(names, run_indices, strict=True))
    stopped = _claimed_tasks_stopped(tm, selected, claimed, expected_runs)
    status = stopped_status if stopped else "unresolved"
    final_detail = detail if stopped else f"{detail}; cleanup could not be confirmed"
    _report(
        receipt_file,
        token=token,
        names=names,
        run_indices=run_indices,
        status=status,
        claimed=claimed,
        detail=final_detail,
    )
    return 2


def main() -> int:
    args = _parse_args()
    web_batch = getattr(args, "web_batch", False)
    tm: TaskManager | None = None
    names: list[str] = []
    run_indices: list[int] = []
    claimed: list[str] = []
    selected: dict[str, dict[str, Any]] = {}
    receipt_file = ""
    payload_file = ""
    token = ""
    accepted_reported = False
    activity = EnvironmentActivityLease("cli-runner")
    try:
        token = validate_submission_token(args.submission_token)
        workspace = os.path.abspath(args.workspace)
        tasks_dir = os.path.join(workspace, TASKS_DIR)
        receipt_file, abort_file = submission_control_paths(tasks_dir, token)
        payload_file = submission_payload_path(tasks_dir, token)
        payload = read_submission_payload(payload_file, token=token)
        names = list(payload.names)
        run_indices = list(payload.run_indices)
        remove_control_file(payload_file)
        if not names or len(names) != len(set(names)) or args.jobs < (0 if web_batch else 1):
            return 2

        activity.start()

        _report(
            receipt_file,
            token=token,
            names=names,
            run_indices=run_indices,
            status="starting",
            claimed=claimed,
        )
        if abort_requested(abort_file, token=token):
            _report(
                receipt_file,
                token=token,
                names=names,
                run_indices=run_indices,
                status="aborted",
                claimed=claimed,
                detail="aborted before task discovery",
            )
            return 2

        os.environ[ENV_KEY_ROOT] = workspace
        ensure_settings_file(workspace)
        load_settings(workspace)
        tm = TaskManager(tasks_dir=tasks_dir, lazy_scan=False, runner_token=token)

        loaded = [tm.get_task(name) for name in names]
        selected = {
            str(task["name"]): task for task in loaded
            if task and task.get("dir") and not task.get("_load_error")
            and str(task.get("status", "") or "").lower() not in _ACTIVE_STATUSES
        }
        if not selected or (not web_batch and len(selected) != len(names)):
            _report(
                receipt_file,
                token=token,
                names=names,
                run_indices=run_indices,
                status="rejected",
                claimed=claimed,
                detail="one or more tasks are unavailable",
            )
            return 2

        if abort_requested(abort_file, token=token):
            _report(
                receipt_file,
                token=token,
                names=names,
                run_indices=run_indices,
                status="aborted",
                claimed=claimed,
                detail="aborted before claiming tasks",
            )
            return 2

        _report(
            receipt_file,
            token=token,
            names=names,
            run_indices=run_indices,
            status="claiming",
            claimed=claimed,
        )
        expected_runs = dict(zip(names, run_indices, strict=True))
        if web_batch:
            claimed = tm.start_batch_tasks(
                list(selected),
                max_workers=args.jobs or None,
                expected_run_indices=expected_runs,
                expected_task_identities=payload.task_identities,
            )
            if not claimed:
                _report(
                    receipt_file, token=token, names=names, run_indices=run_indices,
                    status="rejected", claimed=claimed, detail="no tasks could be claimed",
                )
                return 2
        for name, expected_run_index in (() if web_batch else expected_runs.items()):
            if abort_requested(abort_file, token=token):
                return _stop_and_report(
                    tm,
                    selected=selected,
                    receipt_file=receipt_file,
                    token=token,
                    names=names,
                    run_indices=run_indices,
                    claimed=claimed,
                    stopped_status="aborted",
                    detail="submission aborted while claiming tasks",
                )

            claimed_now = tm.start_batch_tasks(
                [name],
                max_workers=args.jobs,
                expected_run_indices={name: expected_run_index},
                # UI scheduler settings do not enable GPU waiting for CLI submissions.
                gpu_wait_rule=payload.gpu_wait_rule or "off",
            )
            if claimed_now != [name]:
                status = "partial" if claimed else "rejected"
                return _stop_and_report(
                    tm,
                    selected=selected,
                    receipt_file=receipt_file,
                    token=token,
                    names=names,
                    run_indices=run_indices,
                    claimed=claimed,
                    stopped_status=status,
                    detail=f"runner could not claim task: {name}",
                )
            claimed.append(name)
            _report(
                receipt_file,
                token=token,
                names=names,
                run_indices=run_indices,
                status="claiming",
                claimed=claimed,
            )

        if abort_requested(abort_file, token=token):
            return _stop_and_report(
                tm,
                selected=selected,
                receipt_file=receipt_file,
                token=token,
                names=names,
                run_indices=run_indices,
                claimed=claimed,
                stopped_status="aborted",
                detail="submission aborted after claiming tasks",
            )

        _report(
            receipt_file,
            token=token,
            names=names,
            run_indices=run_indices,
            status="accepted" if len(claimed) == len(names) else "accepted_partial",
            claimed=claimed,
        )
        accepted_reported = True
        settled: dict[str, str | None] = {name: "skipped" for name in names if name not in claimed}
        expected_runner_id = str(getattr(tm, "runner_id", "") or "") or None
        poll_interval = _POLL_INTERVAL_SEC
        previous_settled_count = 0
        while True:
            if abort_requested(abort_file, token=token):
                return _stop_and_report(
                    tm,
                    selected=selected,
                    receipt_file=receipt_file,
                    token=token,
                    names=names,
                    run_indices=run_indices,
                    claimed=claimed,
                    stopped_status="aborted",
                    detail="accepted submission was aborted during handoff",
                )

            for name, run_index in zip(names, run_indices, strict=True):
                if name in settled:
                    continue
                info = load_task_metadata(str(selected[name]["dir"])) or {}
                status = _submitted_run_status(
                    info, run_index, expected_runner_id=expected_runner_id, expected_attempt_id=token,
                )
                if status in _ACTIVE_STATUSES:
                    continue
                # A missing or stale snapshot cannot end a run whose local worker
                # is still active. Returning here would trigger shutdown cleanup.
                local_status = tm.local_submission_status(name, run_index, attempt_id=token)
                if local_status in _ACTIVE_STATUSES:
                    continue
                settled[name] = status or local_status
            if len(settled) == len(names):
                if abort_requested(abort_file, token=token):
                    return _stop_and_report(
                        tm,
                        selected=selected,
                        receipt_file=receipt_file,
                        token=token,
                        names=names,
                        run_indices=run_indices,
                        claimed=claimed,
                        stopped_status="aborted",
                        detail="accepted submission was aborted during handoff",
                    )
                return 0 if all(status in {"completed", "skipped"} for status in settled.values()) else 1
            progressed = len(settled) != previous_settled_count
            previous_settled_count = len(settled)
            time.sleep(poll_interval)
            poll_interval = (
                _POLL_INTERVAL_SEC
                if progressed
                else min(_MAX_POLL_INTERVAL_SEC, poll_interval * 1.5)
            )
    except Exception as exc:
        if receipt_file and not accepted_reported:
            try:
                status = "partial" if claimed and len(claimed) < len(names) else "aborted"
                if not claimed:
                    status = "rejected"
                if tm is not None:
                    return _stop_and_report(
                        tm,
                        selected=selected,
                        receipt_file=receipt_file,
                        token=token,
                        names=names,
                        run_indices=run_indices,
                        claimed=claimed,
                        stopped_status=status,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                _report(
                    receipt_file,
                    token=token,
                    names=names,
                    run_indices=run_indices,
                    status=status,
                    claimed=claimed,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                try:
                    _report(
                        receipt_file,
                        token=token,
                        names=names,
                        run_indices=run_indices,
                        status="unresolved",
                        claimed=claimed,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                except Exception:
                    pass
        return 2
    finally:
        if payload_file:
            remove_control_file(payload_file)
        if tm is not None:
            tm.shutdown()
        activity.close()


if __name__ == "__main__":
    raise SystemExit(main())
