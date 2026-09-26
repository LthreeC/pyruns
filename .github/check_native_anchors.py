"""Compare frozen-path behavior against the verified metadata-boundary fix."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pyruns.utils import info_io, task_files  # noqa: E402
from tests.test_workspace_path_safety import _link_directory, _unlink_directory  # noqa: E402

source = subprocess.check_output([
    'git', 'show', '733519318293eaca3901e70227ca72e730f16875:pyruns/utils/info_io.py',
], cwd=ROOT)
baseline = types.ModuleType('pyruns_anchor_before')
exec(compile(source, '<verified-anchor-baseline>', 'exec'), baseline.__dict__)
candidate = info_io._path_is_within
checks = []
skipped = []
traces = []
output = Path(sys.argv[1])
output.parent.mkdir(parents=True, exist_ok=True)

def checkpoint():
    output.write_text(json.dumps({'checks': checks, 'traces': traces, 'skipped': skipped}, indent=2) + '\n', encoding='utf-8')

def capture(call):
    try:
        return {'result': call()}
    except (OSError, ValueError) as exc:
        return {'exception': type(exc).__name__, 'message': str(exc)}

def compare(label, call):
    values = []
    for implementation in (baseline._path_is_within, candidate):
        with patch.object(info_io, '_path_is_within', implementation):
            values.append(capture(call))
    traces.append({'label': label, 'values': values})
    checkpoint()
    assert values[0] == values[1], (label, values)
    checks.append({'label': label, 'equal': True, 'outcome': values[1]})

def payload(directory, filename):
    resolution = capture(lambda: task_files.resolve_task_payload_path(str(directory), filename))
    traces.append({'directory': str(directory), 'file': filename, 'resolution': resolution,
                   'direct_exists': os.path.exists(os.path.join(directory, filename)),
                   'resolved_exists': os.path.exists(resolution['result']) if 'result' in resolution else False})
    checkpoint()
    kind, _config, text, error = task_files.read_task_payload(
        str(directory), {'task_kind': 'shell', 'config_file': filename},
    )
    return kind, text, error

def validate_frozen(directory, filename):
    directory = os.path.abspath(directory)
    anchors = {directory: None}
    info_io.validate_task_directory(directory, _resolved_paths=anchors)
    info_io.validate_workspace_file(os.path.join(directory, filename), directory,
                                    label='Payload', _resolved_paths=anchors)

with tempfile.TemporaryDirectory(prefix='pyruns-native-anchors-') as directory:
    root = Path(directory)
    project = root / 'first'
    relative = Path('_pyruns_') / 'train' / 'tasks' / 'sample'
    task = project / relative
    task.mkdir(parents=True)
    (task / 'run.sh').write_bytes(b'echo original\n')
    (task / 'directory').mkdir()
    outside = root / 'other' / relative
    outside.mkdir(parents=True)
    (outside / 'run.sh').write_bytes(b'echo other\n')
    roots = [('plain', str(task))]
    special_dirs = []
    if os.name == 'nt':
        extended = '\\\\?\\' + str(task)
        roots.append(('extended', extended))
        for suffix in ('literal.', 'literal '):
            special = '\\\\?\\' + str(root) + '\\' + suffix
            os.mkdir(special)
            with open(special + '\\run.sh', 'wb') as stream:
                stream.write(b'echo literal\n')
            roots.append((suffix, special))
            special_dirs.append(special)
        drive, tail = os.path.splitdrive(str(task))
        unc = '\\\\localhost\\' + drive[0] + '$' + tail
        if os.path.isdir(unc):
            roots.append(('UNC', unc))
        else:
            skipped.append('Local administrative UNC share unavailable')
    try:
        for label, task_root in roots:
            for filename in ('run.sh', 'missing.sh', 'missing/child.sh', 'directory', '../escape.sh'):
                compare(f'{label} payload {filename}', lambda r=task_root, f=filename: payload(r, f))
                compare(f'{label} frozen file {filename}', lambda r=task_root, f=filename: validate_frozen(r, f))
        if os.name == 'nt':
            for special in special_dirs:
                compare('missing DOS name against literal ' + special[-1], lambda s=special:
                        info_io.validate_workspace_file(s + '\\run.sh', str(root / 'literal'), label='Payload'))

        for implementation in (baseline._path_is_within, candidate):
            anchors = {str(task): None}
            with patch.object(info_io, '_path_is_within', implementation):
                info_io.validate_task_directory(str(task), _resolved_paths=anchors)
                displaced = root / 'displaced'
                project.rename(displaced)
                _link_directory(project, root / 'other')
                try:
                    outcome = capture(lambda anchors=anchors: info_io.validate_workspace_file(
                        str(task / 'missing.sh'), str(task), label='Payload', _resolved_paths=anchors,
                    ))
                    assert outcome.get('exception') == 'ValueError' and 'resolves outside' in outcome['message'], outcome
                finally:
                    _unlink_directory(project)
                    displaced.rename(project)
        checks.append({'label': 'frozen physical spelling retargeted before missing-file fallback', 'equal': True})
    finally:
        for special in special_dirs:
            os.unlink(special + '\\run.sh')
            os.rmdir(special)

report = {'passed': True, 'comparisons': len(checks), 'checks': checks, 'skipped': skipped,
          'traces': traces,
          'baseline_info_io_sha256': hashlib.sha256(source).hexdigest(),
          'candidate_info_io_sha256': hashlib.sha256(Path(info_io.__file__).read_bytes()).hexdigest()}
output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'passed': True, 'comparisons': len(checks), 'skipped': skipped}))
