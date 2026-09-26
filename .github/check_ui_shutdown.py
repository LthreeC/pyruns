"""Preserve bounded repetitions of the existing real-server handoff test."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

output = Path('test-results')
output.mkdir(exist_ok=True)
observations = []
full_suite = '--full' in sys.argv[1:]
for index in range(1 if full_suite else 10):
    started = time.perf_counter()
    with (output / f'handoff-{index}.log').open('w', encoding='utf-8') as log:
        try:
            result = subprocess.run(
                [sys.executable, '-X', 'utf8', '-u', '-m', 'pytest', '-q',
                 'tests' if full_suite else 'tests/test_web.py::test_live_web_server_gracefully_hands_idle_update_to_replacer',
                 f'--junitxml={output}/handoff-{index}.xml'],
                stdout=log, stderr=subprocess.STDOUT, text=True, timeout=900 if full_suite else 45,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
            returncode = result.returncode
        except subprocess.TimeoutExpired:
            returncode = 124
    observations.append({'iteration': index, 'returncode': returncode,
                         'seconds': time.perf_counter() - started})
    print(json.dumps(observations[-1]), flush=True)
report = {'source_sha': os.environ.get('GITHUB_SHA'), 'scope': 'full' if full_suite else 'handoff',
          'observations': observations,
          'module_sha256': {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
                            for name in ('pyruns/web/app.py', 'pyruns/web/self_update.py', 'tests/test_web.py')}}
(output / 'handoff.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
sys.exit(int(any(row['returncode'] for row in observations)))
