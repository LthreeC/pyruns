"""
Report / Export service — builds CSV/JSON reports from monitor data.

Data-loading utilities (load_monitor_data, get_log_options) live in
pyruns.utils.task_io. This module only contains export business logic.
"""
import io
import csv
import json
from typing import Dict, Any, List

# Imported for internal use
from pyruns.utils.info_io import load_monitor_data
from pyruns.utils import get_now_str


# ═══════════════════════════════════════════════════════════════
#  Export builders
# ═══════════════════════════════════════════════════════════════

def build_export_csv(tasks: List[Dict[str, Any]]) -> str:
    """Build CSV string — one row per task per run.

    Columns: name, id, status, run, start_time, finish_time, pid,
             plus any monitor data keys.
    """
    all_rows: List[Dict[str, Any]] = []
    all_keys: set = set()

    for t in tasks:
        name = t.get("name", "")
        status = t.get("status", "")
        starts = t.get("start_times") or []
        finishes = t.get("finish_times") or []
        pids = t.get("pids") or []
        data = load_monitor_data(t["dir"])

        n_runs = max(len(starts), 1)  # at least 1 row even if never run

        for i in range(n_runs):
            row: Dict[str, Any] = {
                "name": name,
                "status": status,
                "run": i + 1,
                "start_time": starts[i] if i < len(starts) else "",
                "finish_time": finishes[i] if i < len(finishes) else "",
                "pid": pids[i] if i < len(pids) else "",
            }

            # Attach monitor entries that belong to this run (by index).
            # If there are fewer monitor entries than runs, leave blank.
            if i < len(data):
                entry = data[i]
                for k, v in entry.items():
                    row[k] = v

            all_rows.append(row)
            all_keys.update(row.keys())

    if not all_rows:
        return ""

    priority = ["name", "status", "run", "start_time", "finish_time", "pid"]
    cols = [c for c in priority if c in all_keys]
    cols += sorted(all_keys - set(priority))

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for row in all_rows:
        writer.writerow(row)
    return output.getvalue()


def build_export_json(tasks: List[Dict[str, Any]]) -> str:
    """Build JSON string from monitor data of multiple tasks."""
    result = []
    for t in tasks:
        data = load_monitor_data(t["dir"])
        if data:
            result.append({
                "task_name": t.get("name", ""),
                "monitor": data,
            })
    return json.dumps(result, indent=2, ensure_ascii=False)


def export_timestamp() -> str:
    """Return a timestamp string for export filenames."""
    return get_now_str()
