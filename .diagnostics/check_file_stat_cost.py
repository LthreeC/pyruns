"""Full-request ABBA comparison for the temporary file validation prototype."""
import argparse
import gc
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument('--output', required=True)
parser.add_argument('--count', type=int, default=10000)
args = parser.parse_args()
root = Path.cwd()
sys.path.insert(0, str(root))
from fastapi.testclient import TestClient
from pyruns.utils import info_io
from pyruns.web.app import create_app
from pyruns.web.runtime import PyrunsRuntime
from file_stat_prototype import activate, candidate, original, source

spec = importlib.util.spec_from_file_location('scaling', root / 'scripts/benchmark_scaling.py')
scaling = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scaling)
report = {
    'baseline': 'd9eb3cb0cd706e2fc204a2576aa8cf386d1fd986',
    'source': scaling._source_state(), 'tasks': args.count, 'passed': False, 'samples': [],
    'prototype_sha256': hashlib.sha256(source.encode()).hexdigest(),
    'scope': 'Same fixture and machine, ABBA twice; fresh default runtime, warm filesystem cache, full HTTP and all task fields and order compared.',
}
baseline_source = subprocess.check_output(['git', 'show', report['baseline'] + ':pyruns/utils/info_io.py'])
assert baseline_source.replace(b'\r\n', b'\n') == (root / 'pyruns/utils/info_io.py').read_bytes().replace(b'\r\n', b'\n')
output = Path(args.output)
output.parent.mkdir(parents=True, exist_ok=True)
try:
    reference = None
    with tempfile.TemporaryDirectory(prefix='pyruns-file-stat-cost-') as temporary:
        workspace, fixtures = scaling._make_workspace(Path(temporary), args.count)
        for index, task_path in enumerate(sorted((workspace / 'tasks').iterdir())):
            stamp = 1700000000000000000 + index * 1000000000
            os.utime(task_path, ns=(stamp, stamp))
        for variant in ('before', 'after', 'after', 'before') * 2:
            aliases = activate(original if variant == 'before' else candidate)
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
                    priority_response = client.get('/api/tasks', params={**params, 'sort': 'priority'})
                    assert priority_response.status_code == 200
                    state = (payload, tasks, order, priority_response.json())
                    if reference is None:
                        reference = state
                    assert state == reference
                    sample = {'variant': variant, 'first_http_seconds': elapsed,
                              'all_outputs_and_order_equal': True, 'aliases': aliases}
                    report['samples'].append(sample)
                    print(json.dumps(sample), flush=True)
            finally:
                runtime.shutdown()
                output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    report['median_seconds'] = {
        variant: statistics.median(s['first_http_seconds'] for s in report['samples'] if s['variant'] == variant)
        for variant in ('before', 'after')
    }
    report['passed'] = True
finally:
    activate(original)
    output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
print(json.dumps({key: value for key, value in report.items() if key != 'samples'}, indent=2))
