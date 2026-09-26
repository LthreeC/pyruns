"""Run the existing slow-writer regression with per-child lock timing evidence."""
import json
import os
from pathlib import Path
import sys
import time
import traceback

ROOT = Path(os.environ.get("PYRUNS_AUDIT_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))
import pytest
import tests.test_track_store as test_module

output = Path(sys.argv[1]).resolve()
output.mkdir(parents=True, exist_ok=True)
rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 15
original_child = test_module._child
current_round = 0
report = {"passed": False, "requested_rounds": rounds, "rounds": []}


def child(code, *args):
    instrument = f'''
import atexit, contextlib, json, os, pathlib, time
from pyruns.utils import info_io, lock_queue
trace = pathlib.Path({str(output)!r}) / ('round-{current_round:02d}-' + str(os.getpid()) + '.jsonl')
trace_rows = []
@atexit.register
def flush_trace():
    trace.write_text(''.join(json.dumps(row) + '\\n' for row in trace_rows), encoding='utf-8')
original_lock = info_io.task_info_lock
@contextlib.contextmanager
def observed_lock(task_dir, *args, **kwargs):
    started = time.monotonic()
    acquired = None
    row = {{'pid': os.getpid(), 'started': started}}
    try:
        with original_lock(task_dir, *args, **kwargs):
            acquired = time.monotonic()
            yield
    except BaseException as exc:
        row.update(error=repr(exc), failed_at=time.monotonic(),
                   owner=info_io._read_lock_owner(os.path.join(task_dir, info_io._LOCK_FILENAME)),
                   waiters=sorted(p.name for p in pathlib.Path(task_dir, lock_queue._QUEUE_DIR).glob('*.wait')))
        raise
    finally:
        finished = time.monotonic()
        row.update(wait_seconds=(acquired or finished) - started,
                   hold_and_release_seconds=None if acquired is None else finished - acquired)
        trace_rows.append(row)
info_io.task_info_lock = observed_lock
'''
    return original_child(instrument + "\n" + code, *args)


test_module._child = child
try:
    for current_round in range(rounds):
        started = time.monotonic()
        result = pytest.main([
            "tests/test_track_store.py::test_multiple_processes_preserve_every_append_and_record[0.08]",
            "-q", "-o", "faulthandler_timeout=60",
            "--junitxml=" + str(output / f"round-{current_round:02d}.xml"),
        ])
        row = {"round": current_round, "pytest_exit": int(result), "seconds": time.monotonic() - started}
        report["rounds"].append(row)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(row), flush=True)
        if result != 0:
            raise AssertionError("existing multi-process regression failed")
    report["passed"] = True
except BaseException:
    report["error"] = traceback.format_exc()
    raise
finally:
    test_module._child = original_child
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
