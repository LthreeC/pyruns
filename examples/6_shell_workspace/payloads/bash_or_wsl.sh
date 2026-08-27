#!/usr/bin/env bash
set -euo pipefail

echo "shell=bash"
echo "pwd=$(pwd)"
echo "PYRUNS_EXAMPLE_ENV=${PYRUNS_EXAMPLE_ENV:-}"

python_command=python
if ! command -v "$python_command" >/dev/null 2>&1; then
    python_command=python3
fi

"$python_command" - <<'PY'
import os
print("python_env_marker=" + os.environ.get("PYRUNS_EXAMPLE_ENV", ""))
PY
