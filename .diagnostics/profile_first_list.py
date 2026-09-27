"""Current first-list diagnosis using the versioned rich fixture, without changing products or tests."""
import cProfile
import hashlib
import os
import importlib.util
import io
import json
from pathlib import Path
import pstats
import tempfile
import time

repo = Path.cwd()
audit = repo / '.tmp/quality-audit'
audit.mkdir(parents=True, exist_ok=True)
spec = importlib.util.spec_from_file_location('scaling', repo / 'scripts/benchmark_scaling.py')
scaling = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scaling)

from fastapi.testclient import TestClient
from pyruns.web.app import create_app
from pyruns.web.runtime import PyrunsRuntime

profile = cProfile.Profile()
report = {
    'source': scaling._source_state(), 'tasks': 10000, 'passed': False,
    'scope': 'One unprofiled and one profiled first request, fresh default runtime each; same rich fixture in OS temporary storage, warm disk cache, no cross-version latency claim',
    'observations': [],
}
prefix = 'first-list-5d76ad3'
with tempfile.TemporaryDirectory(prefix='pyruns-current-first-list-') as directory:
    workspace, fixtures = scaling._make_workspace(Path(directory), 10000)
    for profiled in (False, True):
        runtime = PyrunsRuntime(str(workspace))
        original = runtime.ensure_tasks_loaded
        if profiled:
            def profiled_ensure(*args, **kwargs):
                return profile.runcall(original, *args, **kwargs)
            runtime.ensure_tasks_loaded = profiled_ensure
        app = create_app(runtime, allow_test_client_bypass=True)
        app.add_middleware(scaling._TestClientScope)
        try:
            with TestClient(app) as client:
                params = {'summary': 'true', 'refresh': 'false', 'sort': 'name_asc', 'offset': 0, 'limit': 50}
                started = time.perf_counter()
                response = client.get('/api/tasks', params=params)
                elapsed = time.perf_counter() - started
                assert response.status_code == 200, response.text[:500]
                scaling._check_page(response.json(), fixtures, params)
                tasks = runtime.task_manager.list_tasks(summary=False)
                assert len(tasks) == 10000
                for task in tasks:
                    scaling._check_task(task, fixtures, summary=False)
                report['observations'].append({'profiled': profiled, 'seconds': elapsed, 'all_tasks_checked': True})
        finally:
            runtime.shutdown()
    report['passed'] = True
report['profiled_load_calls'] = sum(v[1] for k, v in pstats.Stats(profile).stats.items() if k[2] == '_load_task_dir')
report['source_files'] = {name: hashlib.sha256((repo / name).read_bytes()).hexdigest() for name in ('pyruns/core/task_manager.py','pyruns/utils/file_boundary.py','pyruns/utils/info_io.py','pyruns/utils/task_files.py','pyruns/utils/file_io.py','pyruns/utils/config_utils.py')}
profile.dump_stats(str(audit / f'{prefix}.pstats'))
stream = io.StringIO()
stats = pstats.Stats(profile, stream=stream).sort_stats('cumulative')
stats.print_stats(60)
stats.sort_stats('tottime').print_stats(25)
(audit / f'{prefix}-profile.txt').write_text(stream.getvalue(), encoding='utf-8')
(audit / f'{prefix}.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
print(json.dumps(report, indent=2))
