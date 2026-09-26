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
payload_source = subprocess.check_output([
    'git', 'show', '733519318293eaca3901e70227ca72e730f16875:pyruns/utils/task_files.py',
], cwd=ROOT)
baseline_payload = types.ModuleType('pyruns_payload_before')
with patch.dict(sys.modules, {'pyruns.utils.info_io': baseline}):
    exec(compile(payload_source, '<verified-payload-baseline>', 'exec'), baseline_payload.__dict__)
assert baseline_payload.validate_task_directory is baseline.validate_task_directory
candidate_info, candidate_payload = info_io, task_files
checks = []
skipped = []
traces = []
mismatches = []
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
    for modules in ((baseline, baseline_payload), (candidate_info, candidate_payload)):
        with patch.dict(globals(), info_io=modules[0], task_files=modules[1]):
            values.append(capture(call))
    traces.append({'label': label, 'values': values})
    checkpoint()
    equal = values[0] == values[1]
    literal = label.startswith(('literal.', 'literal '))
    expected_fix = False
    if literal and ' payload ' in label:
        filename = label.split(' payload ', 1)[1]
        if filename == 'run.sh':
            assert values[1] == {'result': ('shell', 'echo literal\n', '')}, values
        elif filename in ('missing.sh', 'missing/child.sh', 'directory'):
            assert values[1] == {'result': ('shell', '', f'{filename} is missing')}, values
        else:
            result = values[1]['result']
            assert result[0] == 'shell' and result[1] == '' and 'resolves outside' in result[2], values
        expected_fix = not equal
    elif literal and ' frozen file ' in label:
        filename = label.split(' frozen file ', 1)[1]
        if filename != '../escape.sh':
            assert values[1] == {'result': None}, values
        else:
            assert values[1].get('exception') == 'ValueError' and 'resolves outside' in values[1]['message'], values
        expected_fix = not equal
    if not equal and not expected_fix:
        mismatches.append({'label': label, 'values': values})
    checks.append({'label': label, 'equal': equal, 'literal_name_fix': expected_fix, 'outcome': values[1]})

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
    normalize = getattr(info_io, '_workspace_abspath', os.path.abspath)
    directory = normalize(directory)
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

        for module in (baseline, candidate_info):
            anchors = {str(task): None}
            with patch.dict(globals(), info_io=module):
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

report = {'passed': not mismatches, 'comparisons': len(checks), 'checks': checks, 'skipped': skipped,
          'mismatches': mismatches,
          'traces': traces,
          'baseline_info_io_sha256': hashlib.sha256(source).hexdigest(),
          'candidate_info_io_sha256': hashlib.sha256(Path(info_io.__file__).read_bytes()).hexdigest()}
output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'passed': not mismatches, 'comparisons': len(checks), 'skipped': skipped}))
assert not mismatches, mismatches
