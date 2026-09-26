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
for index in range(10):
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, '-X', 'utf8', '-m', 'pytest', '-q',
         'tests/test_web.py::test_live_web_server_gracefully_hands_idle_update_to_replacer',
         f'--junitxml={output}/handoff-{index}.xml'],
        capture_output=True, text=True, timeout=45,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    (output / f'handoff-{index}.log').write_text(result.stdout + result.stderr, encoding='utf-8')
    observations.append({'iteration': index, 'returncode': result.returncode,
                         'seconds': time.perf_counter() - started})
    print(json.dumps(observations[-1]), flush=True)
report = {'source_sha': os.environ.get('GITHUB_SHA'), 'observations': observations,
          'module_sha256': {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
                            for name in ('pyruns/web/app.py', 'pyruns/web/self_update.py', 'tests/test_web.py')}}
(output / 'handoff.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
sys.exit(int(any(row['returncode'] for row in observations)))
