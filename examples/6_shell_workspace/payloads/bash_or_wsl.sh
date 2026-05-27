#!/usr/bin/env bash
set -euo pipefail

echo "shell=bash"
echo "pwd=$(pwd)"
echo "PYRUNS_EXAMPLE_ENV=${PYRUNS_EXAMPLE_ENV:-}"

python - <<'PY'
import os
print("python_env_marker=" + os.environ.get("PYRUNS_EXAMPLE_ENV", ""))
PY
