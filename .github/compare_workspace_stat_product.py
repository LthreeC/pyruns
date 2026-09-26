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
import time
import traceback

BASELINE = "1eaff82a2def3ff6fd70fc1b8fb54bb164f2d19b"


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
    from pyruns.core.task_manager import TaskManager
    from pyruns.utils import info_io
    from pyruns.web.app import create_app
    from pyruns.web.runtime import PyrunsRuntime

    assert Path(info_io.__file__).resolve().is_relative_to(repo.resolve()), info_io.__file__
    fixtures = json.loads(fixtures_path.read_text(encoding="utf-8"))
    decisions = []
    original_decision = TaskManager._parallel_loading_worthwhile

    def observe(elapsed, cpu):
        result = original_decision(elapsed, cpu)
        decisions.append({"elapsed": elapsed, "cpu": cpu, "parallel": result})
        return result

    TaskManager._parallel_loading_worthwhile = staticmethod(observe)
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
            params["sort"] = "priority"
            priority = client.get("/api/tasks", params=params)
            assert priority.status_code == 200
            snapshot = {"response": response.json(), "tasks": {task["name"]: task for task in tasks},
                        "priority": priority.json()}
            save(output, {"seconds": seconds, "snapshot": snapshot, "validated": True,
                          "raw_order_sha256": scaling._digest([task["name"] for task in tasks]),
                          "snapshot_sha256": scaling._digest(snapshot), "decisions": decisions,
                          "info_io_sha256": hashlib.sha256(Path(info_io.__file__).read_bytes()).hexdigest()})
    finally:
        runtime.shutdown()


def compare(output, count):
    repo = Path(os.environ["PYRUNS_AUDIT_ROOT"]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    scaling = load_scaling(repo)
    report = {"baseline": BASELINE, "candidate": scaling._source_state(), "tasks": count,
              "python": sys.version, "platform": platform.platform(), "dependencies": scaling._dependencies(),
              "passed": False, "observations": []}
    try:
        with tempfile.TemporaryDirectory(prefix="pyruns-workspace-stat-product-") as temporary:
            root = Path(temporary)
            baseline_root = root / "baseline"
            baseline_root.mkdir()
            archive = subprocess.check_output(["git", "archive", BASELINE, "pyruns", "scripts"], cwd=repo)
            with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                source.extractall(baseline_root, filter="data")
            workspace, fixtures = scaling._make_workspace(root, count)
            fixture_path = root / "fixtures.json"
            save(fixture_path, fixtures)
            reference = None
            for index, variant in enumerate(("baseline", "candidate", "candidate", "baseline") * 2):
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
                if reference is None:
                    reference = snapshot
                elif snapshot != reference:
                    save(output / "reference.json", reference)
                    save(output / "different.json", snapshot)
                    raise AssertionError("complete response/task/priority snapshot differs")
                sample["variant"] = variant
                report["observations"].append(sample)
                save(output / "report.json", report)
                print(json.dumps({"index": index, "variant": variant, "seconds": sample["seconds"],
                                  "validated": True}), flush=True)
            report["median_seconds"] = {
                name: statistics.median(row["seconds"] for row in report["observations"] if row["variant"] == name)
                for name in ("baseline", "candidate")
            }
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
        compare(Path(sys.argv[1]).resolve(), int(sys.argv[2]))
