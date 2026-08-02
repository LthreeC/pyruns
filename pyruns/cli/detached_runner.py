"""Hidden worker process used by one-shot Pyruns CLI run commands."""

from __future__ import annotations

import argparse
import json
import os
import time

from pyruns._config import ENV_KEY_ROOT, TASKS_DIR
from pyruns.core.task_manager import TaskManager
from pyruns.utils.info_io import load_task_info
from pyruns.utils.settings import ensure_settings_file, load_settings


_FINAL_STATUSES = {"completed", "failed", "cancelled"}
_ACTIVE_STATUSES = {"queued", "running"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--mode", choices=("thread", "process"), default="thread")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--submission-token", required=True)
    parser.add_argument("--tasks-json", required=True)
    return parser.parse_args()


def _task_names(raw: str) -> list[str]:
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("tasks payload must be a list")
    return [str(item) for item in payload if str(item)]


def main() -> int:
    args = _parse_args()
    names = _task_names(args.tasks_json)
    if not names or args.workers <= 0:
        return 2

    workspace = os.path.abspath(args.workspace)
    tasks_dir = os.path.join(workspace, TASKS_DIR)
    os.environ[ENV_KEY_ROOT] = workspace
    ensure_settings_file(workspace)
    load_settings(workspace)
    tm = TaskManager(tasks_dir=tasks_dir, lazy_scan=False, runner_token=args.submission_token)

    selected = [tm.get_task(name) for name in names]
    if any(
        not task
        or task.get("_load_error")
        or str(task.get("status", "") or "").lower() in _ACTIVE_STATUSES
        for task in selected
    ):
        tm.shutdown()
        return 2

    if len(names) == 1:
        claimed = tm.start_task_now(names[0], execution_mode=args.mode)
    else:
        claimed_names = tm.start_batch_tasks(names, execution_mode=args.mode, max_workers=args.workers)
        claimed = set(claimed_names) == set(names)
    if not claimed:
        tm.shutdown()
        return 2

    try:
        while True:
            statuses = []
            for task in selected:
                info = load_task_info(str(task["dir"])) or {}
                statuses.append(str(info.get("status", "") or "").lower())
            if all(status in _FINAL_STATUSES for status in statuses):
                return 0 if all(status == "completed" for status in statuses) else 1
            time.sleep(0.1)
    finally:
        tm.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
