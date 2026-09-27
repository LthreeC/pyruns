"""Embedded into the installed lifecycle workload after its imports and setup."""
import atexit as _trace_atexit
from collections import defaultdict as _trace_defaultdict
from contextlib import contextmanager as _trace_contextmanager
import hashlib as _trace_hashlib
import json as _trace_json
from pathlib import Path
import sqlite3 as _trace_sqlite
import time as _trace_time

import pyruns as _trace_pyruns
import pyruns.utils.info_io as _trace_info
import pyruns.utils.track_store as _trace_store

_trace_points = []
_trace_current = None
_trace_stack = []
_trace_run = globals()["run"]
_trace_expected_steps = globals()["cfg"].steps
_trace_output = Path(__file__).with_name(f"track-profile-{_trace_run}.json")
_trace_sources = {
    module.__name__: _trace_hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    for module in (_trace_pyruns, _trace_info, _trace_store)
}


@_trace_contextmanager
def _trace_stage(name):
    if _trace_current is None:
        yield
        return
    frame = {"start": _trace_time.perf_counter(), "children": 0.0}
    _trace_stack.append(frame)
    failure = None
    try:
        yield
    except BaseException as error:
        failure = type(error).__name__
        raise
    finally:
        elapsed = _trace_time.perf_counter() - frame["start"]
        assert _trace_stack.pop() is frame
        if _trace_stack:
            _trace_stack[-1]["children"] += elapsed
        record = _trace_current["stages"].setdefault(name, {
            "calls": 0, "inclusive_seconds": 0.0, "exclusive_seconds": 0.0, "errors": {},
        })
        record["calls"] += 1
        record["inclusive_seconds"] += elapsed
        record["exclusive_seconds"] += elapsed - frame["children"]
        if failure:
            record["errors"][failure] = record["errors"].get(failure, 0) + 1


def _trace_call(name, original):
    def measured(*args, **kwargs):
        with _trace_stage(name):
            return original(*args, **kwargs)
    return measured


class _TraceContext:
    def __init__(self, name, original):
        self.name = name
        self.original = original

    def __enter__(self):
        with _trace_stage(self.name + ".enter"):
            return self.original.__enter__()

    def __exit__(self, *exc):
        with _trace_stage(self.name + ".exit"):
            return self.original.__exit__(*exc)


def _trace_context(name, original):
    def measured(*args, **kwargs):
        return _TraceContext(name, original(*args, **kwargs))
    return measured


class _TraceConnection(_trace_sqlite.Connection):
    def execute(self, sql, parameters=()):
        label = sql.strip().split(None, 1)[0].lower()
        if label == "pragma":
            label += "." + sql.split(None, 1)[1].split("=", 1)[0].strip().lower()
        with _trace_stage("sqlite.execute." + label):
            return super().execute(sql, parameters)

    def __exit__(self, *exc):
        label = "commit" if exc[0] is None else "rollback"
        if not self.in_transaction:
            label = "exit_without_transaction"
        with _trace_stage("sqlite." + label):
            return super().__exit__(*exc)

    def close(self):
        with _trace_stage("sqlite.close"):
            return super().close()


_trace_original_connect = _trace_sqlite.connect


def _trace_connect(*args, **kwargs):
    assert "factory" not in kwargs, "Unexpected pre-existing SQLite instrumentation"
    with _trace_stage("sqlite.open"):
        return _trace_original_connect(*args, factory=_TraceConnection, **kwargs)


def _trace_save():
    total = _trace_defaultdict(lambda: {
        "calls": 0, "inclusive_seconds": 0.0, "exclusive_seconds": 0.0, "errors": {},
    })
    for point in _trace_points:
        for name, record in point["stages"].items():
            summary = total[name]
            for key in ("calls", "inclusive_seconds", "exclusive_seconds"):
                summary[key] += record[key]
            for name, value in record["errors"].items():
                summary["errors"][name] = summary["errors"].get(name, 0) + value
    report = {
        "run_index": _trace_run, "expected_steps": _trace_expected_steps, "observed_steps": len(_trace_points),
        "source_sha256": _trace_sources, "points": _trace_points, "totals": dict(total),
        "scope": "Inclusive timings overlap; exclusive timings partition sdk.track time. Checkpoint writes are outside sdk.track timing.",
    }
    temporary = _trace_output.with_suffix(".tmp")
    temporary.write_text(_trace_json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(_trace_output)


_trace_sqlite.connect = _trace_connect
_trace_info.task_info_lock = _trace_context("task_lock", _trace_info.task_info_lock)
_trace_store._connect = _trace_context("store.connection", _trace_store._connect)
for _trace_module, _trace_name, _trace_label in (
    (_trace_info, "load_task_metadata", "metadata.read"),
    (_trace_info, "_write_task_info_unlocked", "metadata.write"),
    (_trace_store, "_database_path", "store.path_validation"),
    (_trace_store, "append_point", "store.append_point"),
    (_trace_store, "prepare_generation", "store.prepare_generation"),
):
    setattr(_trace_module, _trace_name, _trace_call(_trace_label, getattr(_trace_module, _trace_name)))

_trace_original_track = _trace_pyruns.track


def _trace_track(*args, **kwargs):
    global _trace_current
    assert _trace_current is None and not _trace_stack, "Unexpected concurrent SDK writer"
    point = {"step": len(_trace_points), "stages": {}}
    _trace_current = point
    try:
        with _trace_stage("sdk.track"):
            return _trace_original_track(*args, **kwargs)
    finally:
        _trace_current = None
        assert not _trace_stack
        _trace_points.append(point)
        if len(_trace_points) % 100 == 0:
            _trace_save()


_trace_pyruns.track = _trace_track
_trace_atexit.register(_trace_save)
