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
from pyruns.utils.info_io import load_task_info
from pyruns.utils.settings import ensure_settings_file, load_settings


_FINAL_STATUSES = {"completed", "failed", "cancelled"}
_ACTIVE_STATUSES = {"queued", "running"}
_CLEANUP_TIMEOUT_SEC = RUNNER_CLEANUP_TIMEOUT_SEC
_POLL_INTERVAL_SEC = 0.05


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--submission-token", required=True)
    return parser.parse_args()


def _submitted_run_status(info: dict[str, Any], run_index: int) -> str | None:
    """Return only the lifecycle state belonging to the submitted run."""

    run_statuses = info.get("run_statuses", [])
    if isinstance(run_statuses, (list, tuple)) and run_index <= len(run_statuses):
        recorded = str(run_statuses[run_index - 1] or "").lower()
        if recorded in _FINAL_STATUSES:
            return recorded

    current = str(info.get("status", "") or "").lower()
    if current in _ACTIVE_STATUSES and active_task_run_index(info) == run_index:
        return current
    try:
        current_run_index = int(info.get("run_index", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if current in _FINAL_STATUSES and current_run_index == run_index:
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
    requested: set[str] = set()
    deadline = time.monotonic() + max(0.05, _CLEANUP_TIMEOUT_SEC)
    while True:
        unsettled: list[str] = []
        for name in claimed:
            task = selected.get(name) or {}
            task_dir = str(task.get("dir", "") or "")
            info = load_task_info(task_dir) if task_dir else None
            if not info:
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
                    ):
                        requested.add(name)
                except Exception:
                    pass

        if not unsettled:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_INTERVAL_SEC)


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
    expected_runs = dict(zip(names, run_indices))
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
    tm: TaskManager | None = None
    names: list[str] = []
    run_indices: list[int] = []
    claimed: list[str] = []
    selected: dict[str, dict[str, Any]] = {}
    receipt_file = ""
    payload_file = ""
    token = ""
    accepted_reported = False
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
        if not names or len(names) != len(set(names)) or args.jobs <= 0:
            return 2

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
        if any(
            not task
            or not str(task.get("dir", "") or "")
            or task.get("_load_error")
            or str(task.get("status", "") or "").lower() in _ACTIVE_STATUSES
            for task in loaded
        ):
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
        selected = {str(task["name"]): task for task in loaded if task}

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
        for name, expected_run_index in zip(names, run_indices):
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
            status="accepted",
            claimed=claimed,
        )
        accepted_reported = True
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

            infos = [load_task_info(str(selected[name]["dir"])) or {} for name in names]
            if any(not info for info in infos):
                return 1
            statuses = [
                _submitted_run_status(info, run_index)
                for info, run_index in zip(infos, run_indices)
            ]
            if any(status is None for status in statuses):
                return 1
            if all(status in _FINAL_STATUSES for status in statuses):
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
                return 0 if all(status == "completed" for status in statuses) else 1
            time.sleep(_POLL_INTERVAL_SEC)
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


if __name__ == "__main__":
    raise SystemExit(main())
