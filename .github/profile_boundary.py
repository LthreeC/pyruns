"""Compare the actual product function against the exact pre-change function."""
import ast
import gc
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
import types
import xml.etree.ElementTree as ET

ROOT = Path(os.environ.get("PYRUNS_AUDIT_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("scaling", ROOT / "scripts/benchmark_scaling.py")
scaling = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scaling)

from fastapi.testclient import TestClient
from pyruns.utils import info_io
from pyruns.web.app import create_app
from pyruns.web.runtime import PyrunsRuntime

output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
count = int(sys.argv[2]) if len(sys.argv) > 2 else 10000
BASELINE = "e0ab12a003230de6171ad2683f1f044c9dc2dff5"


def source_at(revision, name):
    return subprocess.run(["git", "show", revision + ":" + name], cwd=ROOT,
                          check=True, capture_output=True, text=True, encoding="utf-8").stdout


def load_function(source, name, namespace):
    node = next(node for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<isolated-baseline>", "exec"), namespace)
    return namespace[name]


baseline_source = source_at(BASELINE, "pyruns/utils/info_io.py")
original = load_function(baseline_source, "_path_is_within", dict(vars(info_io)))
candidate = info_io._path_is_within
native_resolve = getattr(os.path, "_getfinalpathname", None)

report = {"source": scaling._source_state(), "platform": platform.platform(), "python": sys.version,
          "dependencies": scaling._dependencies(), "tasks": count, "native_available": native_resolve is not None,
          "baseline_revision": BASELINE,
          "product_sha256": hashlib.sha256(Path(info_io.__file__).read_bytes()).hexdigest(),
          "passed": False, "path_cases": [], "observations": []}


def save():
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def check_path(label, path, root, expected):
    before = original(str(path), str(root))
    after = candidate(str(path), str(root))
    report["path_cases"].append({"case": label, "baseline": before, "candidate": after,
                                 "expected": expected, "equivalent": before == after})
    assert after == expected, (label, before, after, expected)


try:
    with tempfile.TemporaryDirectory(prefix="pyruns-boundary-paths-") as directory:
        base = Path(directory)
        root = base / "中文 workspace"
        root.mkdir()
        (root / "inner").mkdir()
        (root / "inner" / "value.txt").write_text("inside", encoding="utf-8")
        outside = base / "中文 workspace-sibling"
        outside.mkdir()
        (outside / "value.txt").write_text("outside", encoding="utf-8")
        check_path("existing", root / "inner" / "value.txt", root, True)
        check_path("equal root", root, root, True)
        check_path("missing leaf", root / "inner" / "missing.txt", root, True)
        check_path("missing parent", root / "missing" / "missing.txt", root, True)
        check_path("prefix sibling", outside / "value.txt", root, False)
        check_path("dotdot outside", root / ".." / outside.name / "value.txt", root, False)
        if os.name == "nt":
            import _winapi

            alias = base / "project"
            _winapi.CreateJunction(str(root), str(alias))
            try:
                check_path("junction inside", alias / "inner" / "value.txt", root, True)
                check_path("junction missing", alias / "missing.txt", root, True)
            finally:
                os.rmdir(alias)
            _winapi.CreateJunction(str(outside), str(alias))
            try:
                check_path("junction replaced outside", alias / "value.txt", root, False)
            finally:
                os.rmdir(alias)
            extended_root = "\\\\?\\" + str(root)
            check_path("extended root and child", extended_root + "\\inner\\value.txt", extended_root, True)
            check_path("mixed extended input", extended_root + "\\inner\\value.txt", root, True)
            for name in ("trailing.", "trailing "):
                special = extended_root + "\\" + name
                os.mkdir(special)
                try:
                    with open(special + "\\value.txt", "w", encoding="utf-8") as handle:
                        handle.write("special")
                    check_path("special " + repr(name), special + "\\value.txt", extended_root, True)
                    check_path("special versus missing normal parent " + repr(name),
                               special + "\\value.txt", root / "trailing", False)
                finally:
                    os.unlink(special + "\\value.txt")
                    os.rmdir(special)
            unc_root = "\\\\localhost\\" + str(root)[0] + "$" + str(root)[2:]
            if os.path.isdir(unc_root):
                check_path("UNC existing", unc_root + "\\inner\\value.txt", unc_root, True)
                check_path("UNC missing", unc_root + "\\missing.txt", unc_root, True)
            else:
                report["unc_unavailable"] = True
        save()

    if "--checks" in sys.argv:
        import pytest

        if os.name == "nt":
            rejected_source = source_at("a5c7a2f", ".github/profile_boundary.py")
            bad_resolve = load_function(rejected_source, "boundary_path", dict(globals()))
            path_proxy = types.SimpleNamespace(**vars(os.path))
            path_proxy.realpath = bad_resolve
            rejected = types.FunctionType(
                original.__code__, {**original.__globals__, "os": types.SimpleNamespace(path=path_proxy)},
                original.__name__, original.__defaults__, original.__closure__,
            )
            rejected.__kwdefaults__ = original.__kwdefaults__
            info_io._path_is_within = rejected
            negative_xml = output / "rejected-prototype.xml"
            negative = pytest.main([
                "tests/test_workspace_path_safety.py::test_workspace_file_boundary_distinguishes_missing_and_literal_windows_names",
                "-q", "-p", "no:cacheprovider", "--junitxml=" + str(negative_xml),
            ])
            failures = ET.parse(negative_xml).findall(".//failure")
            assert negative == 1 and len(failures) == 1
            assert "DID NOT RAISE" in failures[0].get("message", "")
            report["rejected_prototype_caught_by_new_regression"] = True
            save()

        info_io._path_is_within = candidate
        result = pytest.main(["tests/test_workspace_path_safety.py", "tests/test_config_views.py",
                              "tests/test_transactional_creation.py", "-q",
                              "--junitxml=" + str(output / "paths.xml")])
        report["pytest_exit"] = int(result)
        save()
        assert result == 0

    reference = None
    with tempfile.TemporaryDirectory(prefix="pyruns-boundary-first-load-") as directory:
        workspace, fixtures = scaling._make_workspace(Path(directory), count)
        for variant in ("baseline", "candidate", "candidate", "baseline") * 2:
            info_io._path_is_within = original if variant == "baseline" else candidate
            runtime = PyrunsRuntime(str(workspace))
            observation = {"variant": variant, "validated": False}
            try:
                app = create_app(runtime, allow_test_client_bypass=True)
                app.add_middleware(scaling._TestClientScope)
                with TestClient(app) as client:
                    params = {"summary": "true", "refresh": "false", "sort": "name_asc", "offset": 0, "limit": 50}
                    gc.collect()
                    started = time.perf_counter()
                    response = client.get("/api/tasks", params=params)
                    observation["seconds"] = time.perf_counter() - started
                    assert response.status_code == 200, response.text[:500]
                    payload = response.json()
                    scaling._check_page(payload, fixtures, params)
                    tasks = runtime.task_manager.list_tasks(summary=False)
                    assert len(tasks) == len(fixtures)
                    for task in tasks:
                        scaling._check_task(task, fixtures, summary=False)
                    by_name = {task["name"]: task for task in tasks}
                    assert set(by_name) == set(fixtures)
                    snapshot = {"response": payload, "tasks": by_name}
                    if reference is None:
                        reference = snapshot
                    assert snapshot == reference, "response or full task fields changed"
                    observation["snapshot_sha256"] = scaling._digest(snapshot)
                    observation["raw_order_sha256"] = scaling._digest([task["name"] for task in tasks])
                    # Check the public default sort too, outside the timed request.
                    params["sort"] = "priority"
                    priority = client.get("/api/tasks", params=params)
                    assert priority.status_code == 200
                    if "priority_reference" not in report:
                        report["priority_reference"] = priority.json()
                    assert priority.json() == report["priority_reference"]
                    observation["validated"] = True
            finally:
                runtime.shutdown()
                report["observations"].append(observation)
                save()
                print(json.dumps(observation), flush=True)
    report["median_seconds"] = {
        variant: statistics.median(row["seconds"] for row in report["observations"] if row["variant"] == variant)
        for variant in ("baseline", "candidate")
    }
    report["passed"] = True
except Exception:
    report["error"] = traceback.format_exc()
    raise
finally:
    info_io._path_is_within = candidate
    save()
