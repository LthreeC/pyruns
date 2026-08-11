"""Private on-disk protocol shared by CLI submitters and detached runners."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any


SCHEMA_VERSION = 2
MAX_SUBMISSION_PAYLOAD_BYTES = 32 * 1024 * 1024
RUNNER_CLEANUP_TIMEOUT_SEC = 15.0
# The submitter must outwait cleanup plus one blocking process-stop attempt.
SUBMITTER_ABORT_TIMEOUT_SEC = RUNNER_CLEANUP_TIMEOUT_SEC + 10.0
INTERMEDIATE_RECEIPT_STATUSES = frozenset({"starting", "claiming", "stopping"})
TERMINAL_RECEIPT_STATUSES = frozenset(
    {"accepted", "partial", "rejected", "aborted", "unresolved"}
)
RECEIPT_STATUSES = INTERMEDIATE_RECEIPT_STATUSES | TERMINAL_RECEIPT_STATUSES
_TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")
_REPLACE_RETRY_TIMEOUT_SEC = 1.0
_REPLACE_RETRY_INTERVAL_SEC = 0.01


@dataclass(frozen=True)
class SubmissionReceipt:
    runner_pid: int
    status: str
    claimed: tuple[str, ...]
    unclaimed: tuple[str, ...]
    run_indices: tuple[int, ...]
    detail: str = ""


@dataclass(frozen=True)
class SubmissionPayload:
    names: tuple[str, ...]
    run_indices: tuple[int, ...]


def validate_submission_token(token: str) -> str:
    """Return a canonical token that is safe to embed in a file name."""

    value = str(token or "")
    if _TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid submission token")
    return value


def submission_control_paths(tasks_dir: str, token: str) -> tuple[str, str]:
    """Return the runner-owned receipt and submitter-owned abort paths."""

    safe_token = validate_submission_token(token)
    root = os.path.abspath(tasks_dir)
    return (
        os.path.join(root, f".runner-receipt-{safe_token}.json"),
        os.path.join(root, f".runner-abort-{safe_token}.json"),
    )


def submission_payload_path(tasks_dir: str, token: str) -> str:
    """Return the submitter-authored payload path for one runner."""

    safe_token = validate_submission_token(token)
    return os.path.join(
        os.path.abspath(tasks_dir),
        f".runner-submission-{safe_token}.json",
    )


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    """Durably replace one small JSON control file."""

    target = os.path.abspath(path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    temporary = f"{target}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + _REPLACE_RETRY_TIMEOUT_SEC
        while True:
            try:
                os.replace(temporary, target)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_REPLACE_RETRY_INTERVAL_SEC)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def write_submission_receipt(
    path: str,
    *,
    token: str,
    runner_pid: int,
    status: str,
    names: list[str] | tuple[str, ...],
    run_indices: list[int] | tuple[int, ...],
    claimed: list[str] | tuple[str, ...],
    detail: str = "",
) -> None:
    """Publish one runner-authored ownership snapshot."""

    safe_token = validate_submission_token(token)
    normalized_names = tuple(str(name) for name in names)
    normalized_run_indices = tuple(run_indices)
    claimed_input = tuple(str(name) for name in claimed)
    if (
        not normalized_names
        or any(not name for name in normalized_names)
        or len(normalized_names) != len(set(normalized_names))
    ):
        raise ValueError("receipt task names must be unique and non-empty")
    if (
        len(normalized_run_indices) != len(normalized_names)
        or any(type(value) is not int or value <= 0 for value in normalized_run_indices)
    ):
        raise ValueError("receipt run indices must be positive integers aligned with tasks")
    if (
        len(claimed_input) != len(set(claimed_input))
        or not set(claimed_input).issubset(normalized_names)
    ):
        raise ValueError("claimed tasks must be a unique subset of submitted tasks")
    claimed_set = set(claimed_input)
    claimed_names = tuple(name for name in normalized_names if name in claimed_set)
    if claimed_input != claimed_names:
        raise ValueError("claimed tasks must follow submission order")
    unclaimed_names = tuple(
        name for name in normalized_names if name not in claimed_set
    )
    normalized_status = str(status or "").lower()
    if normalized_status not in RECEIPT_STATUSES:
        raise ValueError(f"invalid receipt status: {status}")
    if type(runner_pid) is not int or runner_pid <= 0:
        raise ValueError("runner PID must be a positive integer")
    _validate_status_partition(normalized_status, claimed_names, unclaimed_names)
    atomic_write_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "submission_token": safe_token,
            "runner_pid": int(runner_pid),
            "status": normalized_status,
            "run_indices": list(normalized_run_indices),
            "claimed": list(claimed_names),
            "unclaimed": list(unclaimed_names),
            "detail": str(detail or ""),
        },
    )


def read_submission_receipt(
    path: str,
    *,
    token: str,
    names: list[str] | tuple[str, ...],
    run_indices: list[int] | tuple[int, ...],
) -> SubmissionReceipt | None:
    """Read a receipt only when its identity and task partition are exact."""

    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    normalized_names = tuple(str(name) for name in names)
    normalized_run_indices = tuple(run_indices)
    if (
        not normalized_names
        or any(not name for name in normalized_names)
        or len(normalized_names) != len(set(normalized_names))
    ):
        return None
    if (
        len(normalized_run_indices) != len(normalized_names)
        or any(type(value) is not int or value <= 0 for value in normalized_run_indices)
    ):
        return None
    expected_token = validate_submission_token(token)
    schema_version = payload.get("schema_version")
    receipt_pid = payload.get("runner_pid")
    status = payload.get("status")
    receipt_run_indices = payload.get("run_indices")
    claimed = payload.get("claimed")
    unclaimed = payload.get("unclaimed")
    if (
        type(schema_version) is not int
        or schema_version != SCHEMA_VERSION
        or payload.get("submission_token") != expected_token
        or type(receipt_pid) is not int
        or receipt_pid <= 0
        or not isinstance(status, str)
        or status not in RECEIPT_STATUSES
        or not isinstance(receipt_run_indices, list)
        or any(type(value) is not int or value <= 0 for value in receipt_run_indices)
        or tuple(receipt_run_indices) != normalized_run_indices
        or not _is_string_list(claimed)
        or not _is_string_list(unclaimed)
    ):
        return None

    claimed_names = tuple(claimed)
    unclaimed_names = tuple(unclaimed)
    if len(set(claimed_names)) != len(claimed_names):
        return None
    if len(set(unclaimed_names)) != len(unclaimed_names):
        return None
    claimed_set = set(claimed_names)
    unclaimed_set = set(unclaimed_names)
    if (
        claimed_set & unclaimed_set
        or claimed_set | unclaimed_set != set(normalized_names)
    ):
        return None
    if claimed_names != tuple(
        name for name in normalized_names if name in claimed_set
    ):
        return None
    if unclaimed_names != tuple(
        name for name in normalized_names if name in unclaimed_set
    ):
        return None
    try:
        _validate_status_partition(status, claimed_names, unclaimed_names)
    except ValueError:
        return None

    detail = payload.get("detail", "")
    if not isinstance(detail, str):
        return None
    return SubmissionReceipt(
        receipt_pid,
        status,
        claimed_names,
        unclaimed_names,
        normalized_run_indices,
        detail,
    )


def write_abort_request(path: str, *, token: str, reason: str) -> None:
    """Persist the submitter's request before attempting process termination."""

    atomic_write_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "submission_token": validate_submission_token(token),
            "status": "requested",
            "reason": str(reason or "aborted"),
            "requester_pid": os.getpid(),
        },
    )


def abort_requested(path: str, *, token: str) -> bool:
    """Return whether a well-formed abort belongs to this submission."""

    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return False
    return bool(
        isinstance(payload, dict)
        and type(payload.get("schema_version")) is int
        and payload.get("schema_version") == SCHEMA_VERSION
        and payload.get("submission_token") == validate_submission_token(token)
        and payload.get("status") == "requested"
    )


def remove_control_file(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def write_submission_payload(
    path: str,
    *,
    token: str,
    names: list[str] | tuple[str, ...],
    run_indices: list[int] | tuple[int, ...],
) -> None:
    """Write one bounded, token-bound task submission payload."""

    safe_token = validate_submission_token(token)
    normalized_names = tuple(str(name) for name in names)
    normalized_run_indices = tuple(run_indices)
    if (
        not normalized_names
        or any(not name for name in normalized_names)
        or len(normalized_names) != len(set(normalized_names))
    ):
        raise ValueError("submission task names must be unique and non-empty")
    if (
        len(normalized_run_indices) != len(normalized_names)
        or any(type(value) is not int or value <= 0 for value in normalized_run_indices)
    ):
        raise ValueError(
            "submission run indices must be positive integers aligned with tasks"
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "submission_token": safe_token,
        "submissions": [
            {"name": name, "run_index": run_index}
            for name, run_index in zip(normalized_names, normalized_run_indices)
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_SUBMISSION_PAYLOAD_BYTES:
        raise ValueError(
            "submission payload is too large "
            f"(max {MAX_SUBMISSION_PAYLOAD_BYTES} bytes)"
        )
    atomic_write_json(path, payload)


def read_submission_payload(path: str, *, token: str) -> SubmissionPayload:
    """Read and strictly validate one bounded task submission payload."""

    expected_token = validate_submission_token(token)
    with open(path, "rb") as handle:
        raw = handle.read(MAX_SUBMISSION_PAYLOAD_BYTES + 1)
    if len(raw) > MAX_SUBMISSION_PAYLOAD_BYTES:
        raise ValueError(
            "submission payload is too large "
            f"(max {MAX_SUBMISSION_PAYLOAD_BYTES} bytes)"
        )
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ValueError("invalid submission payload") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "submission_token", "submissions"}
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("submission_token") != expected_token
        or not isinstance(payload.get("submissions"), list)
    ):
        raise ValueError("invalid submission payload")

    names: list[str] = []
    run_indices: list[int] = []
    for item in payload["submissions"]:
        if not isinstance(item, dict) or set(item) != {"name", "run_index"}:
            raise ValueError("invalid submission payload")
        name = item.get("name")
        run_index = item.get("run_index")
        if (
            not isinstance(name, str)
            or not name
            or type(run_index) is not int
            or run_index <= 0
        ):
            raise ValueError("invalid submission payload")
        names.append(name)
        run_indices.append(run_index)
    if not names or len(names) != len(set(names)):
        raise ValueError("invalid submission payload")
    return SubmissionPayload(tuple(names), tuple(run_indices))


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _validate_status_partition(
    status: str,
    claimed: tuple[str, ...],
    unclaimed: tuple[str, ...],
) -> None:
    if status == "accepted" and (not claimed or unclaimed):
        raise ValueError("accepted receipt must claim every task")
    if status == "partial" and (not claimed or not unclaimed):
        raise ValueError("partial receipt must split claimed and unclaimed tasks")
    if status == "rejected" and (claimed or not unclaimed):
        raise ValueError("rejected receipt cannot claim a task")
