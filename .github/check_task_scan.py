"""Compare actual directory discovery against the exact previous method."""
import ast
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
task_manager = importlib.import_module('pyruns.core.task_manager')

BASELINE = 'e080b596b013428b47f72a72d229d60146f05ea3'
source = subprocess.check_output(['git', 'show', BASELINE + ':pyruns/core/task_manager.py'], cwd=ROOT)
tree = ast.parse(source)
manager_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'TaskManager')
method = next(node for node in manager_class.body if isinstance(node, ast.FunctionDef) and node.name == '_scan_task_dir_names')
namespace = vars(task_manager).copy()
exec(compile(ast.Module(body=[method], type_ignores=[]), BASELINE, 'exec'), namespace)
baseline = namespace['_scan_task_dir_names']
candidate = task_manager.TaskManager._scan_task_dir_names
checks = []
skipped = []


def capture(method, manager, strict):
    try:
        return {'result': method(manager, raise_on_error=strict)}
    except (OSError, ValueError) as error:
        return {'exception': type(error).__name__, 'message': str(error)}


def check(label, tasks):
    manager = object.__new__(task_manager.TaskManager)
    manager.tasks_dir = str(tasks)
    for strict in (False, True):
        old = capture(baseline, manager, strict)
        new = capture(candidate, manager, strict)
        checks.append({'case': label, 'strict': strict, 'baseline': old, 'candidate': new})
        assert old == new, (label, strict, old, new)


def link(path, target, kind):
    if kind == 'junction':
        import _winapi
        _winapi.CreateJunction(str(target), str(path))
    else:
        path.symlink_to(target, target_is_directory=True)


def unlink(path, kind):
    path.rmdir() if kind == 'junction' else path.unlink()


report = {'baseline': BASELINE, 'checks': checks, 'skipped': skipped, 'passed': False}
output = Path(sys.argv[1]).resolve()
output.parent.mkdir(parents=True, exist_ok=True)
try:
    with tempfile.TemporaryDirectory(prefix='pyruns-scan-contract-') as directory:
        base = Path(directory)
        outside = base / 'outside'
        outside.mkdir()
        for managed in (False, True):
            parent = base / ('_pyruns_' if managed else 'plain') / 'workspace'
            tasks = parent / 'tasks'
            tasks.mkdir(parents=True)
            for index, name in enumerate(('alpha', '中文 空格', '_pyruns_', '.hidden', '.trash')):
                child = tasks / name
                child.mkdir()
                os.utime(child, (1700000000 + index, 1700000000 + index))
            (tasks / 'ordinary-file').write_text('not a task', encoding='utf-8')
            check(f'normal managed={managed}', tasks)
            check(f'missing managed={managed}', parent / 'missing')
            check(f'file root managed={managed}', tasks / 'ordinary-file')
            kinds = ('symlink', 'junction') if os.name == 'nt' else ('symlink',)
            for kind in kinds:
                alias = tasks / ('alias-' + kind)
                try:
                    link(alias, outside, kind)
                except OSError as error:
                    skipped.append({'case': f'{kind} managed={managed}', 'reason': str(error)})
                    continue
                try:
                    check(f'task {kind} managed={managed}', tasks)
                finally:
                    unlink(alias, kind)
                for replace in (tasks, parent) + ((parent.parent,) if managed else ()):
                    moved = replace.with_name(replace.name + '-original')
                    assert replace.resolve().is_relative_to(base.resolve())
                    assert moved.resolve().is_relative_to(base.resolve())
                    replace.rename(moved)
                    try:
                        link(replace, outside, kind)
                        try:
                            check(f'ancestor {replace.name} {kind} managed={managed}', tasks)
                        finally:
                            unlink(replace, kind)
                    finally:
                        moved.rename(replace)
            if os.name == 'nt':
                extended = Path('\\\\?\\' + str(tasks))
                special = []
                try:
                    for name in ('trailing.', 'space '):
                        path = extended / name
                        path.mkdir()
                        special.append(path)
                    check(f'extended managed={managed}', extended)
                    check(f'dos special names managed={managed}', tasks)
                finally:
                    for path in special:
                        path.rmdir()
    report['passed'] = True
finally:
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'passed': report['passed'], 'checks': len(checks), 'skipped': skipped}))
