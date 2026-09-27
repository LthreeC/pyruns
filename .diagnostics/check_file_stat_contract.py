"""Compare real file validation and public payload results before changing code."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

parser = argparse.ArgumentParser()
parser.add_argument('--output', required=True)
parser.add_argument('--tests', action='store_true')
args = parser.parse_args()
root = Path.cwd()
sys.path.insert(0, str(root))
from pyruns.utils import config_utils, info_io, task_files
import pyruns.launcher
import pyruns.utils.settings
import pyruns.utils.parse_utils
from file_stat_prototype import activate, candidate, original

checks = []
skipped = []


def capture(function, *args, **kwargs):
    try:
        result = function(*args, **kwargs)
        if isinstance(result, tuple):
            kind, config, text, error = result
            result = {'kind': kind, 'config': config_utils.to_container(config, resolve=False),
                      'config_type': type(config).__name__, 'text': text, 'error': error}
        return {'result': result}
    except Exception as exc:
        return {'exception': type(exc).__name__, 'message': str(exc)}


def compare(label, operation, *args, **kwargs):
    activate(original)
    before = capture(operation, *args, **kwargs)
    activate(candidate)
    after = capture(operation, *args, **kwargs)
    assert before == after, (label, before, after)
    checks.append({'label': label, 'equal': True, 'outcome': before})


def validate(path, root):
    return info_io.validate_workspace_file(str(path), str(root), label='Payload')


report = {'baseline': 'd9eb3cb0cd706e2fc204a2576aa8cf386d1fd986', 'checks': checks,
          'skipped': skipped, 'passed': False}
try:
    if args.tests:
        import pytest
        report['aliases'] = activate(candidate)
        code = pytest.main(['tests/test_workspace_path_safety.py', 'tests/test_bounded_file_io.py',
                            'tests/test_utils.py', 'tests/test_track_store.py',
                            '-q', '--junitxml=' + str(Path(args.output).with_suffix('.xml'))])
        report['pytest_exit_code'] = int(code)
        assert code == pytest.ExitCode.OK, code
    else:
        with tempfile.TemporaryDirectory(prefix='pyruns-file-stat-') as temporary:
            base = Path(temporary)
            workspace = base / '_pyruns_' / '中文 workspace'
            task = workspace / 'tasks' / 'sample'
            task.mkdir(parents=True)
            payload = task / 'config.yaml'
            payload.write_text('value: 7\n', encoding='utf-8')
            outside = base / 'outside.yaml'
            outside.write_text('outside: yes\n', encoding='utf-8')
            for boundary in (task, workspace, base / 'missing-root', payload):
                for path in (payload, task, task / 'missing', payload / 'child', outside, str(task / 'bad') + '\0'):
                    compare(f'file {path!s} / {boundary!s}', validate, path, boundary)
            for target in (payload, outside, task / 'missing-target', task):
                link = task / 'alias'
                try:
                    link.symlink_to(target, target_is_directory=target == task)
                except OSError as exc:
                    skipped.append({'case': 'symlink ' + str(target), 'reason': str(exc)})
                    continue
                try:
                    compare('link ' + str(target), validate, link, task)
                    compare('linked payload ' + str(target), task_files.read_task_payload,
                            str(task), {'config_file': 'alias'})
                finally:
                    link.unlink()
            if hasattr(os, 'mkfifo'):
                fifo = task / 'fifo'
                os.mkfifo(fifo)
                compare('fifo', validate, fifo, task)
                compare('fifo payload', task_files.read_task_payload, str(task), {'config_file': 'fifo'})
            compare('device', validate, os.devnull, os.path.dirname(os.devnull))
            if os.name == 'nt':
                import _winapi
                junction = task / 'junction'
                _winapi.CreateJunction(str(task.parent), str(junction))
                try:
                    compare('junction file', validate, junction, task)
                    compare('junction child', validate, junction / 'sample/config.yaml', task)
                finally:
                    os.rmdir(junction)
                extended = '\\\\?\\' + str(task)
                compare('extended file', validate, extended + '\\config.yaml', task)
                for name in ('trailing.', 'trailing '):
                    special = extended + '\\' + name
                    with open(special, 'w', encoding='utf-8') as handle:
                        handle.write('value: 7\n')
                    try:
                        compare('literal ' + name, validate, special, extended)
                        compare('plain spelling ' + name, validate, task / name, task)
                    finally:
                        os.unlink(special)
            for raw in (b'value: 7\n', b'', b'null', b'[1, 2]', b'value: [broken',
                        b'base: 7\nvalue: ${base}\n', b'value: ???\n', b'1: one\nfalse: no\n',
                        b'value: !!binary YQ==\n', b'value: !!timestamp 2026-09-25\n',
                        b'base: &a {items: [1, 2]}\ncopy: *a\n', b'bad: \xff'):
                payload.write_bytes(raw)
                for view in (False, True):
                    compare('yaml ' + repr(raw) + str(view), task_files.read_task_payload,
                            str(task), {}, config_view=view)
            for error in (PermissionError('temporarily unavailable'), ValueError('invalid path')):
                real_lstat = os.lstat
                def fail_target(path, *args, **kwargs):
                    if os.fspath(path) == str(payload):
                        raise error
                    return real_lstat(path, *args, **kwargs)
                with patch.object(os, 'lstat', fail_target):
                    compare('lstat failure ' + type(error).__name__, validate, payload, task)
            for state in ('missing', 'file', 'directory', 'file'):
                if payload.is_dir():
                    payload.rmdir()
                elif payload.exists():
                    payload.unlink()
                if state == 'file':
                    payload.write_text('value: 7\n', encoding='utf-8')
                elif state == 'directory':
                    payload.mkdir()
                compare('replacement to ' + state, validate, payload, task)
        report['aliases'] = activate(candidate)
    report['passed'] = True
finally:
    activate(original)
    Path(args.output).write_text(json.dumps(report, indent=2, default=repr) + '\n', encoding='utf-8')
print(json.dumps({k: v for k, v in report.items() if k != 'checks'} | {'comparisons': len(checks)}))
