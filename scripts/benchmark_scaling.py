"""Measure real workspace requests and metric storage, with content checks.

Timings describe this checkout and machine. They are observations, not universal
latency limits. Fixture setup, HTTP client JSON decoding, and result validation
are not timed; decoding within metric storage reads is timed.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import gc
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import math
from pathlib import Path
import platform
import statistics
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import tracemalloc
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
STATUSES = ("pending", "queued", "running", "completed", "failed", "cancelled")
STAMP = "2026-01-01_00-00-00"
ENVIRONMENT = {
    "host": "benchmark-host", "system": "benchmark-os", "launcher": "benchmark-python",
    "conda_env": "", "cuda_visible_devices": None, "assigned_gpu_ids": [],
    "gpu_scope": "detected", "gpu_status": "unavailable", "gpus": [],
}


def _write_report(report, output):
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _stats(samples):
    ordered = sorted(samples)
    return {"samples_ms": samples, "median_ms": statistics.median(samples),
            "p95_ms": ordered[math.ceil(0.95 * len(ordered)) - 1], "max_ms": max(samples)}


def _measure(record, call, check, samples, *, memory=True):
    times = record.setdefault("samples_ms", [])
    for _ in range(samples):
        started = time.perf_counter()
        value = call()
        times.append((time.perf_counter() - started) * 1000)
        check(value)
        del value
    record.update(_stats(times))
    if memory:
        gc.collect()
        tracemalloc.start()
        try:
            value = call()
            record["python_peak_bytes"] = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        check(value)
    record["passed"] = True


def _fixture(index):
    status = ("failed", "cancelled", "pending")[index % 10 - 7] if index % 10 >= 7 else "completed"
    ran = status != "pending"
    config = {"epochs": 10, "lr": 0.001, "index": index, "label": "中文样本",
              "nested": {"enabled": True, "layers": [64, 32]}}
    metadata = {
        "name": f"experiment-{index:05d}", "task_kind": "config", "config_file": "config.yaml",
        "status": status, "run_index": int(ran), "created_at": STAMP,
        "pinned": index % 97 == 0, "notes": f"备注 {index}", "env": {"DATASET": "synthetic"},
        "start_times": [STAMP] if ran else [],
        "finish_times": ["2026-01-01_00-00-01"] if ran else [],
        "exit_codes": [{"completed": 0, "failed": 42, "cancelled": 130}[status]] if ran else [],
        "run_statuses": [status] if ran else [], "durations": [1.0] if ran else [],
        "records": [{"score": index, "label": "中文样本"}] if ran else [],
        "tracks": [{"step": [0, 1], "score": [index, index + 1]}] if ran else [],
        "run_environments": [ENVIRONMENT] if ran else [],
    }
    return config, metadata


def _make_workspace(root, count):
    workspace = root / f"workspace-{count}" / "_pyruns_" / "train"
    tasks = workspace / "tasks"
    tasks.mkdir(parents=True)
    fixtures = {}
    for index in range(count):
        config, metadata = _fixture(index)
        task = tasks / metadata["name"]
        task.mkdir()
        # YAML, rather than a JSON-only shortcut, exercises the normal loader.
        (task / "config.yaml").write_text(
            f"epochs: 10\nlr: 0.001\nindex: {index}\nlabel: 中文样本\n"
            "nested:\n  enabled: true\n  layers: [64, 32]\n", encoding="utf-8",
        )
        (task / "task_info.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        if metadata["run_index"]:
            logs = task / "run_logs"
            logs.mkdir()
            (logs / "run1.log").write_text(f"fixture {metadata['name']}\n", encoding="utf-8")
        fixtures[metadata["name"]] = (config, metadata)
    return workspace, fixtures


def _check_task(item, fixtures, *, summary):
    config, metadata = fixtures[item["name"]]
    assert not item.get("_load_error"), (item["name"], item.get("_load_error"))
    # Full pending-task payloads omit the zero run index; summaries include it.
    assert item.get("run_index", 0) == metadata["run_index"], (item["name"], "run_index")
    for key in ("name", "status", "created_at", "pinned", "notes", "env",
                "start_times", "finish_times", "exit_codes", "run_statuses", "durations", "run_environments"):
        assert item[key] == metadata[key], (item["name"], key, item[key], metadata[key])
    assert item["config"] == ({} if summary else config), (item["name"], "config")
    for key in ("records", "tracks"):
        assert item[key] == ([] if summary else metadata[key]), (item["name"], key)


def _check_page(payload, fixtures, params):
    query = params.get("query", "")
    status = params.get("status", "All")
    ordered = sorted(fixtures, key=lambda name: (not fixtures[name][1]["pinned"], name))
    matched = [name for name in ordered if query in name and (status == "All" or fixtures[name][1]["status"] == status)]
    offset, limit = params["offset"], params["limit"]
    expected = matched[offset:offset + limit]
    assert [item["name"] for item in payload["items"]] == expected, "page order or membership differs"
    assert payload["total"] == len(matched), (payload["total"], len(matched))
    assert payload["offset"] == offset and payload["limit"] == limit
    assert payload["has_more"] == (offset + len(expected) < len(matched))
    counts = {state: sum(row[1]["status"] == state for row in fixtures.values()) for state in STATUSES}
    assert payload["status_counts"] == counts, (payload["status_counts"], counts)
    assert payload["search_errors"] == [], payload["search_errors"]
    for item in payload["items"]:
        _check_task(item, fixtures, summary=params["summary"] == "true")
        if query:
            assert item["search_match_count"] == 1
            assert len(item["search_matches"]) == 1
            assert item["search_matches"][0]["field"] == "name"


class _TestClientScope:
    """Support older Starlette test clients that omit their client address."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"} and scope.get("client") is None:
            scope = {**scope, "client": ("testclient", 50000)}
        await self.app(scope, receive, send)


def _workspace_case(root, count, samples, first_samples, record, save):
    from fastapi.testclient import TestClient
    import psutil
    from pyruns.web.app import create_app
    from pyruns.web.runtime import PyrunsRuntime

    workspace, fixtures = _make_workspace(root, count)
    record.update(tasks=count, dataset="mixed terminal/pending config tasks; nested YAML, records, curves, env and logs",
                  cache_state="new runtime for first list; OS file cache warm from fixture creation",
                  requests={}, workspace=str(workspace))
    params = {"summary": "true", "refresh": "false", "sort": "name_asc", "offset": 0, "limit": 50}
    reference = None
    first: dict[str, Any] = {"samples_ms": [], "rss_bytes": []}
    record["requests"]["first_list_new_runtime"] = first
    for _ in range(first_samples):
        runtime = PyrunsRuntime(str(workspace))
        app = create_app(runtime, allow_test_client_bypass=True)
        app.add_middleware(_TestClientScope)
        try:
            with TestClient(app) as client:
                gc.collect()
                before = psutil.Process().memory_info().rss
                started = time.perf_counter()
                response = client.get("/api/tasks", params=params)
                first["samples_ms"].append((time.perf_counter() - started) * 1000)
                first["rss_bytes"].append({"before": before, "after": psutil.Process().memory_info().rss})
                assert response.status_code == 200, response.text[:500]
                payload = response.json()
                _check_page(payload, fixtures, params)
                if reference is None:
                    reference = payload
                assert payload == reference, "fresh runtimes returned different complete responses"
        finally:
            runtime.shutdown()
        save()
    first.update(_stats(first["samples_ms"]), response_sha256=_digest(reference), passed=True)

    runtime = PyrunsRuntime(str(workspace))
    app = create_app(runtime, allow_test_client_bypass=True)
    app.add_middleware(_TestClientScope)
    try:
        with TestClient(app) as client:
            response = client.get("/api/tasks", params=params)
            assert response.status_code == 200 and response.json() == reference
            all_tasks = runtime.task_manager.list_tasks(summary=False)
            assert len(all_tasks) == count
            assert {task["name"] for task in all_tasks} == set(fixtures)
            for task in all_tasks:
                _check_task(task, fixtures, summary=False)
            del all_tasks
            scenarios = [
                ("summary_page", {}),
                ("full_page", {"summary": "false"}),
                ("failed_page", {"status": "failed"}),
                ("name_search_none", {"query": "absent"}),
                ("name_search_partial", {"query": "experiment-000"}),
                ("name_search_all", {"query": "experiment"}),
            ]
            for label, extra in scenarios:
                options = {**params, "offset": 17, "search_field": "name", "include_logs": "true", **extra}
                response = client.get("/api/tasks", params=options)
                assert response.status_code == 200, response.text[:500]
                expected = response.json()
                _check_page(expected, fixtures, options)
                measurement = record["requests"][label] = {
                    "params": options, "matched": expected["total"], "response_sha256": _digest(expected),
                }

                def request(options=options):
                    return client.get("/api/tasks", params=options)

                def check(result, expected=expected):
                    assert result.status_code == 200, result.text[:500]
                    assert result.json() == expected, "complete response changed during stable workload"

                gc.collect()
                _measure(measurement, request, check, samples)
                save()
    finally:
        runtime.shutdown()
    record["passed"] = True


def _curves(steps):
    return [{"step": list(range(steps)), "score": [2 * index for index in range(steps)]}]


def _check_curves(info, expected):
    assert info["tracks"] == expected, "metric values, order, or run slots differ"


def _metric_case(root, steps, samples, read_samples, record, save, *, events=False):
    from pyruns.utils import info_io, track_store

    task = root / f"metrics-{'events' if events else 'snapshot'}-{steps}"
    initial = track_store.INLINE_TRACK_POINTS // 2 if events else steps
    expected = _curves(steps)
    info_io.save_task_info(str(task), {"status": "completed", "run_index": 1,
                                      "records": [{"score": 0}], "tracks": _curves(initial)})
    if events:
        descriptor = info_io.load_task_metadata(str(task), raise_error=True)["track_store"]
        # Fixture setup only. Timed writes below always use the real public storage API.
        with track_store._connect(str(task)) as connection, connection:
            connection.executemany(
                "INSERT INTO points(generation,operation,run_index,payload) VALUES (?,?,?,?)",
                ((descriptor["generation"], f"fixture-{index}", 1,
                  json.dumps({"step": index, "score": 2 * index})) for index in range(initial, steps)),
            )
    _check_curves(info_io.load_task_info(str(task), raise_error=True), expected)
    record.update(existing_steps=steps, fixture="append event rows" if events else "snapshot",
                  setup="untimed; full public read checked before measurements", measurements={})

    def append():
        index = len(expected[0]["step"])
        info_io.append_task_track(str(task), {"step": index, "score": 2 * index})

    def appended(_result):
        index = len(expected[0]["step"])
        expected[0]["step"].append(index)
        expected[0]["score"].append(2 * index)

    before_metadata = (task / "task_info.json").read_bytes()
    measurement: dict[str, Any] = {}
    record["measurements"]["append"] = measurement
    _measure(measurement, append, appended, samples)
    if steps * 2 >= track_store.INLINE_TRACK_POINTS:
        assert (task / "task_info.json").read_bytes() == before_metadata, "external appends rewrote lifecycle metadata"
    _check_curves(info_io.load_task_info(str(task), raise_error=True), expected)
    save()

    def read():
        return info_io.load_task_info(str(task), raise_error=True)

    def check_read(result):
        _check_curves(result, expected)

    measurement = record["measurements"]["full_read"] = {}
    _measure(measurement, read, check_read, read_samples)
    save()

    if events and steps >= 100000:
        ready = threading.Barrier(2)

        def concurrent_read():
            ready.wait(timeout=10)
            started = time.perf_counter()
            result = read()
            return result, started, time.perf_counter()

        intervals = []
        baseline = len(expected[0]["step"])
        measurement = record["measurements"]["append_during_full_read"] = {"samples_ms": []}
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(concurrent_read)
            ready.wait(timeout=10)
            for _ in range(samples):
                started = time.perf_counter()
                append()
                finished = time.perf_counter()
                appended(None)
                intervals.append((started, finished))
                measurement["samples_ms"].append((finished - started) * 1000)
            # result() propagates reader exceptions; a finished thread alone is not success.
            captured, read_start, read_end = future.result(timeout=60)
        captured_steps = len(captured["tracks"][0]["step"])
        assert baseline <= captured_steps <= len(expected[0]["step"])
        _check_curves(captured, [{key: values[:captured_steps] for key, values in expected[0].items()}])
        overlaps = [elapsed for (start, end), elapsed in zip(intervals, measurement["samples_ms"], strict=True)
                    if start < read_end and end > read_start]
        assert overlaps, "no append overlapped the full read; concurrency was not measured"
        measurement.update(_stats(measurement["samples_ms"]), overlapping_appends=len(overlaps),
                           overlap_stats=_stats(overlaps), full_read_ms=(read_end - read_start) * 1000, passed=True)
        _check_curves(read(), expected)
        save()
    record.update(final_steps=len(expected[0]["step"]), metadata_bytes=(task / "task_info.json").stat().st_size,
                  database_bytes=(task / "tracks.sqlite3").stat().st_size if (task / "tracks.sqlite3").exists() else 0,
                  passed=True)


def _migration_case(root, record):
    from pyruns.utils import info_io, track_store

    task = root / "metrics-migration"
    steps = track_store.INLINE_TRACK_POINTS // 2 - 1
    info_io.save_task_info(str(task), {"tracks": _curves(steps)})
    assert not (task / "tracks.sqlite3").exists()
    started = time.perf_counter()
    info_io.append_task_track(str(task), {"step": steps, "score": 2 * steps})
    record.update(existing_steps=steps, elapsed_ms=(time.perf_counter() - started) * 1000)
    _check_curves(info_io.load_task_info(str(task), raise_error=True), _curves(steps + 1))
    assert (task / "tracks.sqlite3").is_file(), "threshold crossing did not exercise external storage"
    record["passed"] = True


def _source_state():
    def git(*arguments):
        return subprocess.check_output(
            ["git", *arguments], cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).strip()

    try:
        return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None}


def _dependencies():
    versions = {"sqlite": sqlite3.sqlite_version}
    for name in ("fastapi", "starlette", "anyio", "httpx", "httpx2", "omegaconf", "psutil", "PyYAML", "regex"):
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = None
    return versions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("all", "workspace", "metrics"), default="all")
    parser.add_argument("--tasks", nargs="+", type=int, default=[1000])
    parser.add_argument("--history-steps", nargs="+", type=int, default=[10000])
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--first-samples", type=int, default=3)
    parser.add_argument("--read-samples", type=int, default=3)
    parser.add_argument("--temp-root", type=Path, help="Parent for the temporary fixture, to select a filesystem")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(*args.tasks, args.samples, args.first_samples, args.read_samples) < 1:
        parser.error("task and sample counts must be positive")
    if min(args.history_steps) < 1024:
        parser.error("history steps must be at least 1024 for the append-event fixture")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": 1, "passed": False, "python": sys.version, "platform": platform.platform(),
              "source_root": str(ROOT), "source": _source_state(), "suite": args.suite, "cases": [],
              "requested_tasks": args.tasks, "requested_history_steps": args.history_steps,
              "dependencies": _dependencies(),
              "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "samples": args.samples, "first_samples": args.first_samples, "read_samples": args.read_samples,
              "timing_scope": "operation only; setup, HTTP client JSON decoding and validation excluded; GC enabled",
              "memory_scope": "separate tracemalloc run, Python peak allocation; RSS fields are process snapshots",
              "percentile_method": "nearest rank", "latency_gate": None}
    initial_threads = set(threading.enumerate())

    def save():
        _write_report(report, output)

    try:
        import pyruns
        import psutil

        assert Path(pyruns.__file__).resolve().parent == ROOT / "pyruns", "not checking the current source tree"
        runtime_digest = hashlib.sha256()
        for path in sorted((ROOT / "pyruns").rglob("*.py")):
            runtime_digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0")
            runtime_digest.update(hashlib.sha256(path.read_bytes()).digest())
        report["runtime_sha256"] = runtime_digest.hexdigest()
        with tempfile.TemporaryDirectory(prefix="pyruns-scaling-", dir=args.temp_root) as directory:
            root = Path(directory).resolve()
            report["temporary_directory"] = str(root)
            partition = max(
                (entry for entry in psutil.disk_partitions(all=True) if root.is_relative_to(Path(entry.mountpoint))),
                key=lambda entry: len(entry.mountpoint), default=None,
            )
            report["filesystem"] = {"mountpoint": partition.mountpoint, "type": partition.fstype} if partition else None
            report["cpu_logical_count"] = psutil.cpu_count()
            report["total_memory_bytes"] = psutil.virtual_memory().total
            save()
            if args.suite in {"all", "workspace"}:
                for count in args.tasks:
                    case = {"kind": "workspace", "passed": False}
                    report["cases"].append(case)
                    _workspace_case(root, count, args.samples, args.first_samples, case, save)
                    save()
                    print(json.dumps({"workspace_tasks": count, "passed": True}), flush=True)
            if args.suite in {"all", "metrics"}:
                case = {"kind": "metric_migration", "passed": False}
                report["cases"].append(case)
                _migration_case(root, case)
                save()
                for steps in (0, *args.history_steps):
                    case = {"kind": "metrics", "passed": False}
                    report["cases"].append(case)
                    _metric_case(root, steps, args.samples, args.read_samples, case, save)
                    save()
                steps = max(args.history_steps)
                case = {"kind": "metrics", "passed": False}
                report["cases"].append(case)
                _metric_case(root, steps, args.samples, args.read_samples, case, save, events=True)
                save()
            deadline = time.monotonic() + 10
            while remaining := [t for t in threading.enumerate() if t not in initial_threads and t.is_alive()]:
                if time.monotonic() >= deadline:
                    raise AssertionError(f"benchmark threads did not exit: {[t.name for t in remaining]}")
                for thread in remaining:
                    thread.join(timeout=0.1)
            report["passed"] = True
    except Exception:
        report["error"] = traceback.format_exc()
        print(report["error"], file=sys.stderr)
    finally:
        save()
    print(json.dumps({"passed": report["passed"], "cases": len(report["cases"]), "output": str(output)}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
