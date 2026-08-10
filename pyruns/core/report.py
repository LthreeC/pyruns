"""
Report / Export service — builds CSV/JSON reports from monitor data.

Data-loading utilities (load_record_data, get_log_options) live in
``pyruns.utils.info_io``. This module only contains export business logic.
"""
import io
import csv
import json
from typing import Dict, Any, List

# Imported for internal use
from pyruns.utils.info_io import load_record_data, run_slot_count
from pyruns.utils import get_now_str


_LIFECYCLE_COLUMNS = {
    "name",
    "status",
    "run",
    "start_time",
    "finish_time",
    "duration_seconds",
    "exit_code",
    "pid",
}


def _run_status(
    task_status: str,
    run_number: int,
    run_count: int,
    exit_code: Any,
    recorded_status: Any,
) -> str:
    normalized_recorded = str(recorded_status or "").strip().lower()
    if normalized_recorded in {"completed", "failed", "cancelled"}:
        return normalized_recorded
    if exit_code is not None and exit_code != "":
        try:
            code = int(exit_code)
        except (TypeError, ValueError):
            return "failed"
        if code == 0:
            return "completed"
        if code == 130:
            return "cancelled"
        return "failed"
    if run_number == run_count and task_status:
        return task_status
    return "unknown"


def _spreadsheet_safe(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return "'" + value
    return value


# ═══════════════════════════════════════════════════════════════
#  Export builders
# ═══════════════════════════════════════════════════════════════

def _build_export_rows(
    tasks: List[Dict[str, Any]],
    *,
    statuses: set[str] | None = None,
) -> List[Dict[str, Any]]:
    """Return one export row per task run, including runs without monitor data."""

    all_rows: List[Dict[str, Any]] = []
    for t in tasks:
        name = t.get("name", "")
        status = str(t.get("status", "") or "").lower()
        starts = t.get("start_times") or []
        finishes = t.get("finish_times") or []
        pids = t.get("pids") or []
        durations = t.get("durations") or []
        exit_codes = t.get("exit_codes") or []
        run_statuses = t.get("run_statuses") or []
        data = load_record_data(t["dir"])

        n_runs = max(run_slot_count(t), len(data))

        for i in range(n_runs):
            exit_code = exit_codes[i] if i < len(exit_codes) else ""
            row: Dict[str, Any] = {
                "name": name,
                "status": _run_status(
                    status,
                    i + 1,
                    n_runs,
                    exit_code,
                    run_statuses[i] if i < len(run_statuses) else "",
                ),
                "run": i + 1,
                "start_time": starts[i] if i < len(starts) else "",
                "finish_time": finishes[i] if i < len(finishes) else "",
                "duration_seconds": durations[i] if i < len(durations) else "",
                "exit_code": exit_code,
                "pid": pids[i] if i < len(pids) else "",
            }

            # Attach monitor entries that belong to this run (by index).
            # If there are fewer monitor entries than runs, leave blank.
            if i < len(data):
                entry = data[i]
                if isinstance(entry, dict):
                    for k, v in entry.items():
                        if isinstance(k, str) and k not in _LIFECYCLE_COLUMNS:
                            row[k] = v

            if statuses is None or row["status"] in statuses:
                all_rows.append(row)

    return all_rows


def build_export_csv(
    tasks: List[Dict[str, Any]],
    *,
    statuses: set[str] | None = None,
) -> str:
    """Build CSV string — one row per task per run.

    Columns: name, status, run, start_time, finish_time, duration_seconds,
             exit_code, pid,
             plus any monitor data keys.
    """
    all_rows = _build_export_rows(tasks, statuses=statuses)

    if not all_rows:
        return ""

    all_keys = {key for row in all_rows for key in row}

    priority = [
        "name",
        "status",
        "run",
        "start_time",
        "finish_time",
        "duration_seconds",
        "exit_code",
        "pid",
    ]
    cols = [c for c in priority if c in all_keys]
    cols += sorted(all_keys - set(priority))

    safe_columns: Dict[str, str] = {}
    used_columns: set[str] = set()
    for column in cols:
        safe_column = str(_spreadsheet_safe(column))
        while safe_column in used_columns:
            safe_column = "'" + safe_column
        safe_columns[column] = safe_column
        used_columns.add(safe_column)

    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=[safe_columns[column] for column in cols],
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in all_rows:
        writer.writerow(
            {
                safe_columns[key]: _spreadsheet_safe(value)
                for key, value in row.items()
                if key in safe_columns
            }
        )
    return output.getvalue()


def build_export_json(
    tasks: List[Dict[str, Any]],
    *,
    statuses: set[str] | None = None,
) -> str:
    """Build JSON with the same one-row-per-run semantics as CSV export."""

    try:
        return json.dumps(
            _build_export_rows(tasks, statuses=statuses),
            indent=2,
            ensure_ascii=True,
            allow_nan=False,
        )
    except ValueError as exc:
        raise ValueError("Export contains values that are not valid strict JSON") from exc


def export_timestamp() -> str:
    """Return a timestamp string for export filenames."""
    return get_now_str()
