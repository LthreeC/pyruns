"""
Shared task sorting — used by both Manager and Monitor pages.

Sort key: latest activity timestamp (newest first).
Priority: run_at[-1] > created_at.
"""
import re
import yaml
from typing import Any, Dict


def task_sort_key(task: Dict[str, Any]) -> tuple:
    """Return a tuple for sorting tasks by status priority and timestamps.

    Manager and Monitor use `reverse=True` so higher tuple values appear first.
    
    Priority:
      1. running (50)
      2. queued  (40)
      3. failed  (30)
      4. completed(20)
      5. pending (10)
      
    Secondary:
      - Active tasks (running/queued): earliest timestamp first => Return inverted int.
      - Inactive tasks: latest timestamp first => Return raw string.
    """
    status = task.get("status", "pending")
    priorities = {
        "running": 50,
        "queued": 40,
        "failed": 30,
        "completed": 20,
        "pending": 10
    }
    priority = priorities.get(status, 0)

    starts = task.get("start_times") or []
    ts_str = starts[-1] if isinstance(starts, list) and starts else task.get("created_at", "")

    if priority >= 40:  # running or queued (earliest first)
        digits = "".join(filter(str.isdigit, ts_str))
        secondary = -int(digits) if digits else 0
    else:               # others (latest first)
        secondary = ts_str

    return (priority, secondary)


def filter_tasks(all_tasks: list, query: str, status_mode: str = "All") -> list:
    """Apply status + deep search filters.
    
    Searches both task name and the serialized configuration dictionary.
    """
    tasks = [
        t for t in all_tasks
        if status_mode == "All" or status_mode.lower() == t.get("status", "")
    ]
    if query:
        # User may paste multi-line YAML blocks:
        # device: null
        # batch_size: 32
        # We ensure EVERY non-empty line exists in the task's YAML representation.

        query_lines = [line.strip().lower() for line in query.split('\n') if line.strip()]
        if query_lines:
            def matches_all(t):
                # Dump config to yaml to match standard YAML strings like 'device: null'
                try:
                    yaml_str = yaml.dump(t.get("config", {}), default_flow_style=False).lower()
                except Exception:
                    yaml_str = str(t.get("config", {})).lower()

                text_blob = t.get("name", "").lower() + "\n" + yaml_str
                # For robust matching, remove all spaces around colons in both texts
                # so "device: null" matches "device:null" etc.

                blob_norm = re.sub(r'\s*:\s*', ':', text_blob)
                
                for q_line in query_lines:
                    q_norm = re.sub(r'\s*:\s*', ':', q_line)
                    if q_norm not in blob_norm:
                        return False
                return True
                
            tasks = [t for t in tasks if matches_all(t)]
    return tasks

