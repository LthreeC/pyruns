"""Compare complete product imports in separate processes on one dataset."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback

BASELINE = "6f38ed7478e886d1069e13cd31cb785c069d7122"


def load_scaling(repo):
    sys.path.insert(0, str(repo))
    spec = importlib.util.spec_from_file_location("scaling", repo / "scripts/benchmark_scaling.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def worker(repo, workspace, fixtures_path, output):
    scaling = load_scaling(repo)
    from fastapi.testclient import TestClient
    from pyruns.core import task_manager
    from pyruns.core.task_manager import TaskManager
    from pyruns.utils import info_io
    from pyruns.web.app import create_app
    from pyruns.web.runtime import PyrunsRuntime

    assert Path(info_io.__file__).resolve().is_relative_to(repo.resolve()), info_io.__file__
    fixtures = json.loads(fixtures_path.read_text(encoding="utf-8"))
    decisions, pool_phases, directory_scans = [], [], []
    context = threading.local()
    original_decision = TaskManager._parallel_loading_worthwhile
    original_map = TaskManager._map_task_disk_io
    original_pool = task_manager.ThreadPoolExecutor

    def observe(elapsed, cpu):
        result = original_decision(elapsed, cpu)
        decisions.append({"phase": getattr(context, "phase", ""),
                          "elapsed": elapsed, "cpu": cpu, "threshold_matched": result})
        return result

    def map_io(self, items, load):
        previous = getattr(context, "phase", "")
        context.phase = load.__qualname__
        try:
            result = original_map(self, items, load)
            if context.phase == "TaskManager._scan_task_dir_names.<locals>.inspect_entry":
                directory_scans.append([row for row in result if row is not None])
            return result
        finally:
            context.phase = previous

    def pool(*args, **kwargs):
        pool_phases.append(getattr(context, "phase", ""))
        return original_pool(*args, **kwargs)

    TaskManager._parallel_loading_worthwhile = staticmethod(observe)
    TaskManager._map_task_disk_io = map_io
    task_manager.ThreadPoolExecutor = pool
    runtime = PyrunsRuntime(str(workspace))
    try:
        app = create_app(runtime, allow_test_client_bypass=True)
        app.add_middleware(scaling._TestClientScope)
        with TestClient(app) as client:
            params = {"summary": "true", "refresh": "false", "sort": "name_asc", "offset": 0, "limit": 50}
            started = time.perf_counter()
            response = client.get("/api/tasks", params=params)
            seconds = time.perf_counter() - started
            assert response.status_code == 200, response.text[:500]
            scaling._check_page(response.json(), fixtures, params)
            tasks = runtime.task_manager.list_tasks(summary=False)
            assert len(tasks) == len(fixtures)
            for task in tasks:
                scaling._check_task(task, fixtures, summary=False)
            assert len(directory_scans) == 1, len(directory_scans)
            expected_order = [name for name, _ in sorted(directory_scans[0], key=lambda row: row[1], reverse=True)]
            assert [task["name"] for task in tasks] == expected_order
            params["sort"] = "priority"
            priority = client.get("/api/tasks", params=params)
            assert priority.status_code == 200
            snapshot = {"response": response.json(), "tasks": {task["name"]: task for task in tasks},
                        "priority": priority.json()}
            save(output, {"seconds": seconds, "snapshot": snapshot, "validated": True,
                          "raw_order_sha256": scaling._digest([task["name"] for task in tasks]),
                          "directory_scan": directory_scans[0],
                          "snapshot_sha256": scaling._digest(snapshot), "decisions": decisions, "pool_phases": pool_phases,
                          "task_manager_sha256": hashlib.sha256(Path(sys.modules[TaskManager.__module__].__file__).read_bytes()).hexdigest()})
    finally:
        runtime.shutdown()
        TaskManager._parallel_loading_worthwhile = staticmethod(original_decision)
        TaskManager._map_task_disk_io = original_map
        task_manager.ThreadPoolExecutor = original_pool


def compare(output, count, order_control=False):
    repo = Path(os.environ["PYRUNS_AUDIT_ROOT"]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    scaling = load_scaling(repo)
    report = {"baseline": BASELINE, "candidate": scaling._source_state(), "tasks": count,
              "mode": "same-source-order-control" if order_control else "stable-directory-times-abba",
              "python": sys.version, "platform": platform.platform(), "dependencies": scaling._dependencies(),
              "passed": False, "observations": []}
    try:
        with tempfile.TemporaryDirectory(prefix="pyruns-discovery-refresh-product-") as temporary:
            root = Path(temporary)
            baseline_root = root / "baseline"
            baseline_root.mkdir()
            archive = subprocess.check_output(["git", "archive", BASELINE, "pyruns", "scripts"], cwd=repo)
            with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                source.extractall(baseline_root, filter="data")
            report["source_hashes"] = {
                variant: hashlib.sha256((source_root / "pyruns/core/task_manager.py").read_bytes()).hexdigest()
                for variant, source_root in (("baseline", baseline_root), ("candidate", repo))
            }
            workspace, fixtures = scaling._make_workspace(root, count)
            if not order_control:
                # Untimed fixture preparation: establish the same explicit order
                # before either product reads the directory enumeration metadata.
                for index, name in enumerate(fixtures):
                    stamp = (1_700_000_000 + index) * 1_000_000_000
                    os.utime(workspace / "tasks" / name, ns=(stamp, stamp))
                report["expected_order_sha256"] = scaling._digest(list(reversed(fixtures)))
            fixture_path = root / "fixtures.json"
            save(fixture_path, fixtures)
            reference = None
            reference_order = None
            variants = ("baseline", "baseline") if order_control else ("baseline", "candidate", "candidate", "baseline") * 2
            for index, variant in enumerate(variants):
                source_root = baseline_root if variant == "baseline" else repo
                result_path = root / f"sample-{index}.json"
                result = subprocess.run(
                    [sys.executable, "-u", str(Path(__file__).resolve()), "--worker", str(source_root),
                     str(workspace), str(fixture_path), str(result_path)], cwd=source_root,
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240,
                )
                (output / f"worker-{index}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
                result.check_returncode()
                sample = json.loads(result_path.read_text(encoding="utf-8"))
                snapshot = sample.pop("snapshot")
                sample["variant"] = variant
                report["observations"].append(sample)
                assert sample["task_manager_sha256"] == report["source_hashes"][variant], sample
                if reference is None:
                    reference = snapshot
                    reference_order = sample["raw_order_sha256"]
                elif snapshot != reference:
                    save(output / "reference.json", reference)
                    save(output / "different.json", snapshot)
                    raise AssertionError("complete response/task/priority snapshot differs")
                if not order_control:
                    assert sample["raw_order_sha256"] == reference_order == report["expected_order_sha256"], sample
                save(output / "report.json", report)
                print(json.dumps({"index": index, "variant": variant, "seconds": sample["seconds"],
                                  "validated": True}), flush=True)
            report["median_seconds"] = {
                name: statistics.median(row["seconds"] for row in report["observations"] if row["variant"] == name)
                for name in sorted(set(variants))
            }
            if order_control:
                first, second = report["observations"]
                report["raw_order_equal"] = first["raw_order_sha256"] == second["raw_order_sha256"]
                first_times, second_times = dict(first["directory_scan"]), dict(second["directory_scan"])
                report["changed_directory_times"] = [
                    {"name": name, "first": value, "second": second_times[name]}
                    for name, value in first_times.items() if second_times[name] != value
                ]
        report["passed"] = True
    except Exception:
        report["error"] = traceback.format_exc()
        raise
    finally:
        save(output / "report.json", report)
    print(json.dumps({"passed": True, "median_seconds": report["median_seconds"]}))


if __name__ == "__main__":
    if sys.argv[1] == "--worker":
        worker(*(Path(value) for value in sys.argv[2:]))
    else:
        compare(Path(sys.argv[1]).resolve(), int(sys.argv[2]), "--order-control" in sys.argv[3:])
