"""Compare consecutive real HTTP reads after a slow refresh, in separate processes."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
import zipfile


ROOT = Path(__file__).resolve().parents[1]
BASELINE = 'e5ea3b2a3d4d77b83eb91b4217d7482776471fe1'


def child(args):
    source = Path(args.source).resolve()
    sys.path.insert(0, str(source))
    sys.path.insert(1, str(source / 'scripts'))
    import pyruns
    from benchmark_scaling import _check_page, _check_task, _digest, _fixture, _TestClientScope
    from fastapi.testclient import TestClient
    from pyruns.web.app import create_app
    from pyruns.web.runtime import PyrunsRuntime

    assert Path(pyruns.__file__).resolve().parent == source / 'pyruns'
    fixtures = {}
    for index in range(args.tasks):
        config, metadata = _fixture(index)
        fixtures[metadata['name']] = (config, metadata)
    runtime = PyrunsRuntime(args.workspace)
    calls = []
    background_calls = []
    original = runtime.task_manager.refresh_from_disk

    def counted(*call_args, **kwargs):
        started = time.perf_counter()
        result = original(*call_args, **kwargs)
        elapsed = time.perf_counter() - started
        if kwargs.get('check_all') and kwargs.get('discover'):
            calls.append(elapsed)
        else:
            background_calls.append(elapsed)
        return result

    runtime.task_manager.refresh_from_disk = counted
    app = create_app(runtime, allow_test_client_bypass=True)
    app.add_middleware(_TestClientScope)
    params = {'summary': 'true', 'refresh': 'false', 'sort': 'name_asc', 'offset': 0, 'limit': 50}
    try:
        with TestClient(app) as client:
            initial = client.get('/api/tasks', params=params)
            assert initial.status_code == 200
            _check_page(initial.json(), fixtures, params)
            calls.clear()
            background_calls.clear()
            started = time.perf_counter()
            forced = client.get('/api/tasks', params={**params, 'force_refresh': 'true'})
            forced_seconds = time.perf_counter() - started
            started = time.perf_counter()
            following = client.get('/api/tasks', params={**params, 'refresh': 'true'})
            following_seconds = time.perf_counter() - started
            result = {
                'label': args.label, 'forced_seconds': forced_seconds,
                'following_seconds': following_seconds, 'refresh_seconds': calls,
                'background_refresh_count': len(background_calls),
                'statuses': [initial.status_code, forced.status_code, following.status_code],
                'response_digests': [hashlib.sha256(response.content).hexdigest()
                                     for response in (initial, forced, following)],
                'passed': False,
            }
            Path(args.output).write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
            if initial.content != forced.content or forced.content != following.content:
                for label, response in zip(('initial', 'forced', 'following'), (initial, forced, following), strict=True):
                    Path(args.output).with_suffix('.' + label + '.json').write_bytes(response.content)
            assert forced.status_code == following.status_code == 200
            assert initial.content == forced.content == following.content
            _check_page(following.json(), fixtures, params)
            all_tasks = runtime.task_manager.list_tasks(summary=False)
            assert len(all_tasks) == args.tasks
            for task in all_tasks:
                _check_task(task, fixtures, summary=False)
            snapshot = _digest(sorted(all_tasks, key=lambda task: task['name']))
            slow = calls[0] >= 4
            expected_calls = 2 if args.label == 'baseline' and slow else 1
            assert len(calls) == expected_calls, (args.label, calls)
            result = {
                'label': args.label, 'forced_seconds': forced_seconds,
                'following_seconds': following_seconds, 'refresh_seconds': calls,
                'background_refresh_count': len(background_calls),
                'slow_refresh': slow, 'expected_refresh_calls': expected_calls,
                'response_sha256': hashlib.sha256(initial.content).hexdigest(),
                'snapshot_sha256': snapshot,
                'runtime_sha256': hashlib.sha256((source / 'pyruns/web/runtime.py').read_bytes()).hexdigest(),
                'passed': True,
            }
    finally:
        runtime.shutdown()
    Path(args.output).write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')


def parent(args):
    sys.path.insert(0, str(ROOT / 'scripts'))
    from benchmark_scaling import _make_workspace

    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'baseline': BASELINE, 'tasks': args.tasks, 'source': str(ROOT),
              'python': sys.version, 'platform': sys.platform, 'runs': [], 'passed': False}
    try:
        with tempfile.TemporaryDirectory(prefix='pyruns-refresh-completion-') as temp:
            root = Path(temp)
            baseline = root / 'baseline'
            baseline.mkdir()
            archive = subprocess.check_output(['git', 'archive', '--format=zip', BASELINE], cwd=ROOT)
            with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                zipped.extractall(baseline)
            workspace, _fixtures = _make_workspace(root, args.tasks)
            for index, label in enumerate(('baseline', 'candidate', 'candidate', 'baseline')):
                path = output.parent / f'round-{index}-{label}.json'
                process = subprocess.run([
                    sys.executable, str(Path(__file__).resolve()), '--child',
                    '--source', str(baseline if label == 'baseline' else ROOT),
                    '--workspace', str(workspace), '--tasks', str(args.tasks),
                    '--label', label, '--output', str(path),
                ], capture_output=True, encoding='utf-8', errors='replace', timeout=240)
                path.with_suffix('.stdout.log').write_text(process.stdout, encoding='utf-8')
                path.with_suffix('.stderr.log').write_text(process.stderr, encoding='utf-8')
                process.check_returncode()
                result = json.loads(path.read_text(encoding='utf-8'))
                report['runs'].append(result)
                output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
            assert len({run['response_sha256'] for run in report['runs']}) == 1
            assert len({run['snapshot_sha256'] for run in report['runs']}) == 1
            report['followup_median_seconds'] = {
                label: statistics.median(run['following_seconds'] for run in report['runs'] if run['label'] == label)
                for label in ('baseline', 'candidate')
            }
            report['passed'] = True
    except Exception:
        report['error'] = traceback.format_exc()
        raise
    finally:
        output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--source')
    parser.add_argument('--workspace')
    parser.add_argument('--label')
    parser.add_argument('--tasks', type=int, default=10000)
    parser.add_argument('--output', required=True)
    arguments = parser.parse_args()
    child(arguments) if arguments.child else parent(arguments)
