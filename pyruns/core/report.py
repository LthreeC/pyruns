"""
Report / Export utilities – loads monitor data and builds CSV/JSON reports.
"""
import io
import csv
import json
import datetime
import os
from typing import Dict, Any, List

from pyruns._config import INFO_FILENAME, MONITOR_KEY, LOG_FILENAME, RERUN_LOG_DIR


# ═══════════════════════════════════════════════════════════════
#  Monitor data I/O
# ═══════════════════════════════════════════════════════════════

def load_monitor_data(task_dir: str) -> List[Dict[str, Any]]:
    """Load monitor entries from task_info.json."""
    info_path = os.path.join(task_dir, INFO_FILENAME)
    try:
        with open(info_path, "r", encoding="utf-8") as f:
            info = json.load(f)
        return info.get(MONITOR_KEY, [])
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════
#  Log file discovery
# ═══════════════════════════════════════════════════════════════

def get_log_options(task_dir: str) -> Dict[str, str]:
    """Return {display_name: file_path} for run.log + rerunX.log."""
    opts: Dict[str, str] = {}
    run_log = os.path.join(task_dir, LOG_FILENAME)
    if os.path.exists(run_log):
        opts["run.log"] = run_log
    rerun_dir = os.path.join(task_dir, RERUN_LOG_DIR)
    if os.path.isdir(rerun_dir):
        files = sorted(
            [f for f in os.listdir(rerun_dir) if f.startswith("rerun") and f.endswith(".log")],
            key=lambda x: int("".join(filter(str.isdigit, x)) or "0"),
        )
        for f in files:
            opts[f] = os.path.join(rerun_dir, f)
    return opts


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

    # Column order: _task_name, _task_id, _ts, then alphabetically
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

