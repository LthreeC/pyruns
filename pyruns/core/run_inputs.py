"""Immutable, on-demand run inputs; never included in task list payloads."""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from pyruns.utils.file_boundary import validate_open_file
from pyruns.utils.file_io import read_bounded_bytes, regular_file_opener
from pyruns.utils.info_io import (
    MAX_RUN_HISTORY_SLOTS,
    prepare_task_log_path,
    task_info_lock,
    validate_task_log_path,
)
from pyruns.utils.text_diff import unified_text_diff

MAX_RUN_INPUT_BYTES = 16 * 1024 * 1024
_INPUT_FIELDS = (
    "task_kind", "payload_file", "payload_text", "script", "command",
    "workdir", "shell_executable", "environment",
)


def materialize_config_run_input(task_dir: str, config_file: str, config: Any) -> tuple[str, str]:
    """Capture the resolved task configuration for this Python run."""
    from omegaconf import OmegaConf

    from pyruns.utils.task_files import MAX_TASK_PAYLOAD_BYTES, resolve_task_payload_path

    resolve_task_payload_path(task_dir, config_file)
    text = OmegaConf.to_yaml(config, resolve=False)
    if len(text.encode("utf-8")) > MAX_TASK_PAYLOAD_BYTES:
        raise ValueError(f"Task payload is too large (max {MAX_TASK_PAYLOAD_BYTES} bytes)")
    fd, temporary = tempfile.mkstemp(prefix=".pyruns-input-", suffix=".yaml", dir=task_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    except BaseException:
        os.remove(temporary)
        raise
    return text, temporary


def _filename(run_index: int) -> str:
    if isinstance(run_index, bool) or not isinstance(run_index, int) or not 1 <= run_index <= MAX_RUN_HISTORY_SLOTS:
        raise ValueError(f"Run must be between 1 and {MAX_RUN_HISTORY_SLOTS}")
    return f"run{run_index}.input.json"


def save_run_input(
    task_dir: str, run_index: int, data: dict[str, Any], *, runner_id: str = "",
    expected_attempt_id: str | None = None,
) -> None:
    """Publish one complete input atomically, refusing to replace an earlier run."""
    filename = _filename(run_index)
    payload = {**data, "schema_version": 1, "run_index": run_index}
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
    if len(raw) > MAX_RUN_INPUT_BYTES:
        raise ValueError(f"Run input is too large (max {MAX_RUN_INPUT_BYTES} bytes)")
    with task_info_lock(task_dir, create_dir=False):
        if runner_id or expected_attempt_id is not None:
            from pyruns.utils.info_io import load_task_metadata, task_attempt_matches

            info = load_task_metadata(task_dir, raise_error=True)
            if (
                (runner_id and (info.get("runner_id") != runner_id or info.get("run_index") != run_index))
                or not task_attempt_matches(info, expected_attempt_id)
            ):
                raise RuntimeError("Task ownership changed before saving run input")
        path = prepare_task_log_path(task_dir, filename)
        if os.path.lexists(path):
            raise FileExistsError(f"Run input already exists: {path}")
        fd, temporary = tempfile.mkstemp(prefix=".input-", suffix=".tmp", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            prepare_task_log_path(task_dir, filename)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)


def load_run_input(task_dir: str, run_index: int) -> dict[str, Any] | None:
    """Return a saved input, or None for histories without a snapshot."""
    path = validate_task_log_path(task_dir, _filename(run_index))
    boundary = os.path.realpath(os.path.dirname(path))
    try:
        with open(path, "rb", opener=regular_file_opener) as handle:
            validate_open_file(handle, path, boundary)
            raw = read_bounded_bytes(handle, MAX_RUN_INPUT_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_RUN_INPUT_BYTES:
        raise ValueError(f"Run input is too large (max {MAX_RUN_INPUT_BYTES} bytes)")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("run_index") != run_index:
        raise ValueError("Invalid run input snapshot")
    if not isinstance(data.get("payload_text"), str) or not isinstance(data.get("environment"), dict):
        raise ValueError("Invalid run input payload or environment")
    return data


def compare_run_inputs(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
    *,
    left_label: str,
    right_label: str,
) -> dict[str, Any]:
    """Compare authored inputs, excluding capture timestamps and temporary launch paths."""
    available = left is not None and right is not None
    result: dict[str, Any] = {
        "schema_version": 1,
        "left": {"reference": left_label, "available": left is not None},
        "right": {"reference": right_label, "available": right is not None},
        "available": available,
        "equal": None,
        "changed_fields": [],
        "diff": "",
        "truncated": False,
    }
    if not available:
        return result
    assert left is not None and right is not None
    def canonical(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)

    changed = [field for field in _INPUT_FIELDS if canonical(left.get(field)) != canonical(right.get(field))]
    if not changed:
        result["equal"] = True
        return result

    def text(data: dict[str, Any]) -> str:
        metadata = {field: data.get(field) for field in _INPUT_FIELDS if field != "payload_text"}
        header = json.dumps(metadata, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        # Keep script/config lines readable instead of JSON-escaping newlines.
        return header + "\n\n--- payload ---\n" + data["payload_text"]

    diff, truncated = unified_text_diff(text(left), text(right), left_label=left_label, right_label=right_label)
    result.update(
        equal=False,
        changed_fields=changed,
        diff=diff,
        truncated=truncated,
    )
    return result
