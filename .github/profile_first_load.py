"""Observe the existing first-list workload and its disk-I/O decisions."""
import cProfile
import importlib.util
import io
import json
import os
from pathlib import Path
import pstats
import sys
import tempfile
import threading
import time

REPO = Path(os.environ.get("PYRUNS_AUDIT_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO))
spec = importlib.util.spec_from_file_location("scaling", REPO / "scripts/benchmark_scaling.py")
scaling = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scaling)

from fastapi.testclient import TestClient
from pyruns.core.task_manager import TaskManager
from pyruns.web.app import create_app
from pyruns.web.runtime import PyrunsRuntime

output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
count = int(sys.argv[2]) if len(sys.argv) > 2 else 10000
report = {"tasks": count, "source": scaling._source_state(), "passed": False, "observations": []}
profile = cProfile.Profile()
original_map = TaskManager._map_task_disk_io
original_decision = TaskManager._parallel_loading_worthwhile
context = threading.local()


def decision(elapsed, cpu):
    parallel = original_decision(elapsed, cpu)
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
        for profiled in (False, True):
            observations = {"profiled": profiled, "phases": [], "validated": False}
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
                    observations["validated"] = True
            finally:
                runtime.shutdown()
                report["observations"].append(observations)
                (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report["passed"] = True
finally:
    TaskManager._map_task_disk_io = original_map
    TaskManager._parallel_loading_worthwhile = staticmethod(original_decision)
    profile.dump_stats(str(output / "first-load.pstats"))
    stream = io.StringIO()
    stats = pstats.Stats(profile, stream=stream).sort_stats("cumulative")
    stats.print_stats(45)
    stats.sort_stats("tottime").print_stats(25)
    (output / "profile.txt").write_text(stream.getvalue(), encoding="utf-8")
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, indent=2))
