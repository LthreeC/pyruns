"""
CLI entry point — ``pyr <script.py>`` or ``pyr help``.
"""

import os
import sys
import textwrap
import traceback

from ._config import ENV_ROOT, ENV_SCRIPT, DEFAULT_ROOT_NAME, CONFIG_DEFAULT_FILENAME
from . import __version__ as _VERSION
from . import ensure_config_default 

# ─── Help text ────────────────────────────────────────────────

_HELP = textwrap.dedent(
    f"""
pyruns v{_VERSION}

USAGE
    pyr <script.py>        Start UI for script (e.g. `pyr train.py`)
    pyr -h | -v            Show help / version

MODE 1: ARGPARSE (Zero Config)
    If your script uses `argparse`, just run it! Params are auto-detected.
    $ pyr train.py

MODE 2: MANUAL CONFIG
    If not using argparse, you must provide a default config file:

    project/
    ├── {DEFAULT_ROOT_NAME}/
    │   └── {CONFIG_DEFAULT_FILENAME}   <-- Your params (YAML)
    └── train.py

    Then in your script:
    import pyruns
    args = pyruns.load()  # Returns params from UI or config_default.yaml
    """.strip()
)


def _print_help():
    print(_HELP)
    sys.exit(0)


def _print_version():
    print(f"pyruns {_VERSION}")
    sys.exit(0)


# ─── Main entry ───────────────────────────────────────────────


def pyr():
    """``pyr <script.py>`` — launch the experiment management UI."""

    if len(sys.argv) < 2:
        _print_help()

    arg = sys.argv[1]

    # Help / version flags
    if arg in ("help", "-h", "--help"):
        _print_help()
    if arg in ("version", "-v", "--version"):
        _print_version()

    # ── Dev mode: pyr dev <script.py> ──
    if arg == "dev":
        if len(sys.argv) < 3:
            print("Usage: pyr dev <script.py>")
            sys.exit(1)
        _launch_dev(sys.argv[2])
        return

    # ── Resolve script path ──
    filepath = os.path.abspath(arg)
    if not os.path.exists(filepath):
        print(f"Error: '{arg}' not found.")
        sys.exit(1)

    try:
        _setup_env(filepath)

        # ── Launch UI ──
        sys.argv = [sys.argv[0]]
        from pyruns.ui.app import main

        main()

    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)


def _setup_env(filepath: str) -> str:
    """Detect config source, ensure workspace, set env vars.

    Returns the pyruns root directory path.
    """
    from pyruns.utils.parse_utils import (
        detect_config_source_fast,
        extract_argparse_params,
        generate_config_file,
        resolve_config_path,
    )
    from pyruns.utils.settings import ensure_settings_file

    # ── Detect config source ──
    mode, extra = detect_config_source_fast(filepath)
    
    print("mode", mode, extra)

    file_dir = os.path.dirname(filepath)
    pyruns_dir = os.path.join(file_dir, DEFAULT_ROOT_NAME)

    if mode == "argparse":
        params = extract_argparse_params(filepath)
        generate_config_file(filepath, params)

    elif mode == "pyruns_read":
        if extra:
            config_file = resolve_config_path(extra, file_dir)
            print(extra, file_dir, config_file)

            if not config_file:
                print(f"Error: Config '{extra}' not found.")
                sys.exit(1)

    os.makedirs(pyruns_dir, exist_ok=True)
    ensure_settings_file()
    ensure_config_default()
    

    # ── Set environment ──
    os.environ[ENV_ROOT] = pyruns_dir
    os.environ[ENV_SCRIPT] = filepath
    return pyruns_dir


def _launch_dev(script_arg: str):
    """Launch UI in dev mode with hot-reload via subprocess."""
    import subprocess as _sp

    filepath = os.path.abspath(script_arg)
    if not os.path.exists(filepath):
        print(f"Error: '{script_arg}' not found.")
        sys.exit(1)

    _setup_env(filepath)
    print(f"[pyruns dev] Hot-reload enabled — editing .py files will auto-restart")
    print(f"[pyruns dev] Script: {filepath}")
    _sp.run([sys.executable, "-m", "pyruns.ui.app"])


if __name__ == "__main__":
    pyr()

