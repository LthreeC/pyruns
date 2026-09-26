import json
import os
from pathlib import Path
import tempfile

from pyruns._config import CONFIG_FILENAME
from pyruns.core.task_manager import TaskManager
from pyruns.utils.task_files import read_task_payload_snapshot

fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
with tempfile.TemporaryDirectory(prefix="pyruns-signature-") as directory:
    task = Path(directory) / "tasks" / "sample"
    task.mkdir(parents=True)
    payload = task / CONFIG_FILENAME
    payload.write_text("value: 7\n", encoding="utf-8")
    snapshot = read_task_payload_snapshot(
        str(task), {"task_kind": "config", "config_file": CONFIG_FILENAME}, config_view=True,
    )
    path_stat = os.stat(payload)
    with payload.open("rb") as handle:
        handle_stat = os.fstat(handle.fileno())
    probed = TaskManager._payload_signature(str(task), CONFIG_FILENAME)
    report = {
        "fields": fields,
        "path_stat": [getattr(path_stat, field) for field in fields],
        "handle_stat": [getattr(handle_stat, field) for field in fields],
        "snapshot_attributes": snapshot.signature[0],
        "current_probe_attributes": probed[0],
        "old_path_probe_equals_snapshot": (
            tuple(getattr(path_stat, field) for field in fields) == snapshot.signature[0]
        ),
        "current_probe_equals_snapshot": probed == snapshot.signature,
        "load_error": snapshot.load_error,
    }
output = Path("test-results/payload-file-attributes.json")
output.parent.mkdir(exist_ok=True)
output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(output.read_text(encoding="utf-8"))
assert not snapshot.load_error
assert report["current_probe_equals_snapshot"]
