"""Observe the existing first-list workload and its disk-I/O decisions."""
import cProfile
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import pstats
import statistics
import sys
import tempfile
import threading
import time
import traceback

REPO = Path(os.environ.get("PYRUNS_AUDIT_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO))
spec = importlib.util.spec_from_file_location("scaling", REPO / "scripts/benchmark_scaling.py")
scaling = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scaling)

from fastapi.testclient import TestClient
from pyruns.core.task_manager import TaskManager
from pyruns.utils import info_io
from pyruns.web.app import create_app
from pyruns.web.runtime import PyrunsRuntime

output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
count = int(sys.argv[2]) if len(sys.argv) > 2 else 10000
mode = sys.argv[3] if len(sys.argv) > 3 else "profile"
report = {"tasks": count, "mode": mode, "source": scaling._source_state(),
          "product_revision": "958c8ed1ea3011f5d750c988af88c1d794020566",
          "platform": platform.platform(), "python": sys.version,
          "dependencies": scaling._dependencies(), "passed": False, "observations": []}
profile = cProfile.Profile()
original_map = TaskManager._map_task_disk_io
original_decision = TaskManager._parallel_loading_worthwhile
original_workspace_check = info_io.validate_workspace_directory
context = threading.local()
policy = "auto"


def decision(elapsed, cpu):
    parallel = policy == "parallel" or original_decision(elapsed, cpu)
    if getattr(context, "phase", None) is not None:
        context.phase["samples"].append({"elapsed": elapsed, "cpu": cpu, "parallel": parallel})
    return parallel


def map_io(self, items, load):
    phase = {"operation": load.__qualname__, "items": len(items), "samples": []}
    previous = getattr(context, "phase", None)
    context.phase = phase
    started = time.perf_counter()
    try:
        return original_map(self, items, load)
    finally:
        phase["seconds"] = time.perf_counter() - started
        observations["phases"].append(phase)
        context.phase = previous


TaskManager._parallel_loading_worthwhile = staticmethod(decision)
TaskManager._map_task_disk_io = map_io
try:
    with tempfile.TemporaryDirectory(prefix="pyruns-first-load-profile-") as directory:
        workspace, fixtures = scaling._make_workspace(Path(directory), count)
        if mode == "workspace-stat":
            from workspace_stat_prototype import validate_workspace_directory as candidate_workspace_check
            variants = [(name, False) for name in ("baseline", "candidate", "candidate", "baseline") * 2]
        elif mode == "profile":
            variants = [("auto", False), ("auto", True)]
        else:
            variants = [(name, False) for name in ("auto", "parallel", "parallel", "auto")]
        reference = None
        for variant, profiled in variants:
            policy = "auto" if mode == "workspace-stat" else variant
            if mode == "workspace-stat":
                info_io.validate_workspace_directory = (
                    original_workspace_check if variant == "baseline" else candidate_workspace_check
                )
            observations = {"variant": variant, "policy": policy, "profiled": profiled,
                            "phases": [], "validated": False}
            runtime = PyrunsRuntime(str(workspace))
            original_ensure = runtime.ensure_tasks_loaded
            if profiled:
                def profiled_ensure(*args, **kwargs):
                    return profile.runcall(original_ensure, *args, **kwargs)
                runtime.ensure_tasks_loaded = profiled_ensure
            app = create_app(runtime, allow_test_client_bypass=True)
            app.add_middleware(scaling._TestClientScope)
            try:
                with TestClient(app) as client:
                    params = {"summary": "true", "refresh": "false", "sort": "name_asc", "offset": 0, "limit": 50}
                    started = time.perf_counter()
                    response = client.get("/api/tasks", params=params)
                    observations["seconds"] = time.perf_counter() - started
                    assert response.status_code == 200, response.text[:500]
                    scaling._check_page(response.json(), fixtures, params)
                    tasks = runtime.task_manager.list_tasks(summary=False)
                    assert len(tasks) == count
                    for task in tasks:
                        scaling._check_task(task, fixtures, summary=False)
                    snapshot = {"response": response.json(), "tasks": {task["name"]: task for task in tasks}}
                    if reference is None:
                        reference = snapshot
                    else:
                        if snapshot != reference:
                            (output / "reference.json").write_text(json.dumps(reference, indent=2), encoding="utf-8")
                            (output / "different.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
                        assert snapshot == reference
                    observations["snapshot_sha256"] = hashlib.sha256(
                        json.dumps(snapshot, sort_keys=True).encode()
                    ).hexdigest()
                    observations["raw_order_sha256"] = scaling._digest([task["name"] for task in tasks])
                    params["sort"] = "priority"
                    priority = client.get("/api/tasks", params=params)
                    assert priority.status_code == 200
                    if "priority_reference" not in report:
                        report["priority_reference"] = priority.json()
                    assert priority.json() == report["priority_reference"]
                    observations["validated"] = True
            finally:
                runtime.shutdown()
                report["observations"].append(observations)
                (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if mode == "workspace-stat":
            report["median_seconds"] = {
                name: statistics.median(row["seconds"] for row in report["observations"] if row["variant"] == name)
                for name in ("baseline", "candidate")
            }
    report["passed"] = True
except Exception:
    report["error"] = traceback.format_exc()
    raise
finally:
    TaskManager._map_task_disk_io = original_map
    info_io.validate_workspace_directory = original_workspace_check
    TaskManager._parallel_loading_worthwhile = staticmethod(original_decision)
    if mode == "profile":
        profile.dump_stats(str(output / "first-load.pstats"))
        stream = io.StringIO()
        stats = pstats.Stats(profile, stream=stream).sort_stats("cumulative")
        stats.print_stats(70)
        stats.sort_stats("tottime").print_stats(35)
        (output / "profile.txt").write_text(stream.getvalue(), encoding="utf-8")
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, indent=2))
