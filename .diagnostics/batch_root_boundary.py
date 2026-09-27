"""Same-process old/new task loading comparison; temporary audit, not a test matrix."""
import argparse
import ast
import gc
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import tempfile
import time
import traceback

parser = argparse.ArgumentParser()
parser.add_argument('--baseline', default='5d76ad3beb3a6e5b93e5f1f95a998c10b496e073')
parser.add_argument('--output', required=True)
parser.add_argument('--count', type=int, default=10000)
parser.add_argument('--negative', action='store_true')
args = parser.parse_args()
root = Path.cwd()
output = Path(args.output)
output.parent.mkdir(parents=True, exist_ok=True)
import sys
sys.path.insert(0, str(root))

from fastapi.testclient import TestClient
from pyruns.core import task_manager
from pyruns.utils import info_io, task_files, file_boundary
from pyruns.web.app import create_app
from pyruns.web.runtime import PyrunsRuntime

spec = importlib.util.spec_from_file_location('scaling', root / 'scripts/benchmark_scaling.py')
scaling = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scaling)
targets = [
    (task_manager.TaskManager, '_load_task_dirs', 'pyruns/core/task_manager.py', 'TaskManager'),
    (task_manager.TaskManager, '_load_task_dir', 'pyruns/core/task_manager.py', 'TaskManager'),
]
current, old, sources = {}, {}, {}
for owner, name, path, cls in targets:
    current[name] = getattr(owner, name)
    source = subprocess.check_output(['git', 'show', f'{args.baseline}:{path}'], text=True, encoding='utf-8')
    nodes = ast.parse(source).body
    if cls:
        nodes = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == cls).body
    node = next(n for n in nodes if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = {}
    module = task_manager if cls else owner
    exec(compile(ast.Module(body=[node], type_ignores=[]), f'{args.baseline}/{path}', 'exec'), module.__dict__, namespace)
    old[name] = namespace[name]
    sources[path] = {'baseline_sha256': hashlib.sha256(source.encode()).hexdigest(),
                     'candidate_sha256': hashlib.sha256((root/path).read_bytes()).hexdigest()}

def activate(methods):
    for owner, name, _path, _cls in targets:
        setattr(owner, name, methods[name])

report = {'baseline': args.baseline, 'source': scaling._source_state(), 'sources': sources, 'file_boundary_sha256': hashlib.sha256((root/'pyruns/utils/file_boundary.py').read_bytes()).hexdigest(),
          'passed': False, 'samples': [], 'tasks': args.count,
          'scope': 'Same fixture and machine, ABBA twice; fresh default runtime, warm filesystem cache, full HTTP and unsorted task snapshots checked.'}
try:
    if args.negative:
        import pytest
        activate(old)
        result = pytest.main(['tests/test_workspace_path_safety.py::test_task_scan_keeps_project_boundary_and_recovers_on_next_scan', 'tests/test_core.py::test_task_manager_reports_metadata_errors_and_recovers[initial_unavailable]', '-q', '--tb=short'])
        report['old_regression_exit_code'] = int(result)
        assert result == pytest.ExitCode.TESTS_FAILED, result
        report['passed'] = True
        output.write_text(json.dumps(report, indent=2) + '\n')
        raise SystemExit(0)
    reference = full_reference = order_reference = priority_reference = None
    with tempfile.TemporaryDirectory(prefix='pyruns-task-boundary-') as temporary:
        workspace, fixtures = scaling._make_workspace(Path(temporary), args.count)
        for index, task_path in enumerate(sorted((workspace/'tasks').iterdir())):
            stamp = 1700000000000000000 + index * 1000000000
            os.utime(task_path, ns=(stamp, stamp))
        for variant in ('before', 'after', 'after', 'before') * 2:
            activate(old if variant == 'before' else current)
            info_io._managed_ancestor_paths.cache_clear()
            runtime = PyrunsRuntime(str(workspace))
            app = create_app(runtime, allow_test_client_bypass=True)
            app.add_middleware(scaling._TestClientScope)
            try:
                with TestClient(app) as client:
                    params = {'summary': 'true', 'refresh': 'false', 'sort': 'name_asc', 'offset': 0, 'limit': 50}
                    gc.collect()
                    started = time.perf_counter()
                    response = client.get('/api/tasks', params=params)
                    elapsed = time.perf_counter() - started
                    assert response.status_code == 200, response.text[:300]
                    payload = response.json()
                    scaling._check_page(payload, fixtures, params)
                    tasks = runtime.task_manager.list_tasks(summary=False)
                    assert len(tasks) == args.count
                    for task in tasks:
                        scaling._check_task(task, fixtures, summary=False)
                    order = [task['name'] for task in runtime.task_manager.tasks]
                    priority_response = client.get('/api/tasks', params={**params, 'sort':'priority'})
                    assert priority_response.status_code == 200, priority_response.text[:300]
                    priority = priority_response.json()
                    if reference is None:
                        reference, full_reference, order_reference, priority_reference = payload, tasks, order, priority
                    assert payload == reference
                    assert tasks == full_reference
                    assert order == order_reference
                    assert priority == priority_reference
                    sample = {'variant': variant, 'first_http_seconds': elapsed, 'all_outputs_and_order_equal': True}
                    report['samples'].append(sample)
                    print(json.dumps(sample), flush=True)
            finally:
                runtime.shutdown()
                output.write_text(json.dumps(report, indent=2) + '\n')
    report['median_seconds'] = {variant: statistics.median(s['first_http_seconds'] for s in report['samples'] if s['variant']==variant)
                                for variant in ('before','after')}
    report['passed'] = True
except Exception:
    report['error'] = traceback.format_exc()
    raise
finally:
    activate(current)
    output.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({k: v for k, v in report.items() if k not in {'sources', 'samples'}}, indent=2))
