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


# ═══════════════════════════════════════════════════════════════
#  Export builders
# ═══════════════════════════════════════════════════════════════

def _build_export_rows(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return one export row per task run, including runs without monitor data."""

    all_rows: List[Dict[str, Any]] = []
    for t in tasks:
        name = t.get("name", "")
        status = t.get("status", "")
        starts = t.get("start_times") or []
        finishes = t.get("finish_times") or []
        pids = t.get("pids") or []
        durations = t.get("durations") or []
        exit_codes = t.get("exit_codes") or []
        data = load_record_data(t["dir"])

        n_runs = max(run_slot_count(t), 1)  # at least 1 row even if never run

        for i in range(n_runs):
            row: Dict[str, Any] = {
                "name": name,
                "status": status,
                "run": i + 1,
                "start_time": starts[i] if i < len(starts) else "",
                "finish_time": finishes[i] if i < len(finishes) else "",
                "duration_seconds": durations[i] if i < len(durations) else "",
                "exit_code": exit_codes[i] if i < len(exit_codes) else "",
                "pid": pids[i] if i < len(pids) else "",
            }

            # Attach monitor entries that belong to this run (by index).
            # If there are fewer monitor entries than runs, leave blank.
            if i < len(data):
                entry = data[i]
                for k, v in entry.items():
                    row[k] = v

            all_rows.append(row)

    return all_rows


def build_export_csv(tasks: List[Dict[str, Any]]) -> str:
    """Build CSV string — one row per task per run.

    Columns: name, status, run, start_time, finish_time, duration_seconds,
             exit_code, pid,
             plus any monitor data keys.
    """
    all_rows = _build_export_rows(tasks)

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

    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=cols,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in all_rows:
        writer.writerow(row)
    return output.getvalue()


def build_export_json(tasks: List[Dict[str, Any]]) -> str:
    """Build JSON with the same one-row-per-run semantics as CSV export."""

    return json.dumps(_build_export_rows(tasks), indent=2, ensure_ascii=False)


def export_timestamp() -> str:
    """Return a timestamp string for export filenames."""
    return get_now_str()
