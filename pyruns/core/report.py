"""
Report / Export service — builds CSV/JSON reports from monitor data.

Data-loading utilities (load_monitor_data, get_log_options) live in
pyruns.utils.task_io. This module only contains export business logic.
"""
import io
import csv
import json
import datetime
from typing import Dict, Any, List

# Re-export from utils.task_io for backward compatibility
from pyruns.utils.task_io import load_monitor_data, get_log_options  # noqa: F401


# ═══════════════════════════════════════════════════════════════
#  Export builders
# ═══════════════════════════════════════════════════════════════

def build_export_csv(tasks: List[Dict[str, Any]]) -> str:
    """Build CSV string from monitor data of multiple tasks."""
    all_rows: List[Dict[str, Any]] = []
    all_keys: set = set()
    for t in tasks:
        data = load_monitor_data(t["dir"])
        for entry in data:
            row = {"_task_name": t.get("name", ""), "_task_id": t.get("id", "")}
            row.update(entry)
            all_rows.append(row)
            all_keys.update(row.keys())

    if not all_rows:
        return ""

    priority = ["_task_name", "_task_id", "_ts"]
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
                "task_id": t.get("id", ""),
                "monitor": data,
            })
    return json.dumps(result, indent=2, ensure_ascii=False)


def export_timestamp() -> str:
    """Return a timestamp string for export filenames."""
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
