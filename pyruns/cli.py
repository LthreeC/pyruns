"""
CLI entry point — ``pyr <script.py>`` or ``pyr help``.
"""

import os
import sys
import textwrap
import traceback

from ._config import ENV_KEY_ROOT, ENV_KEY_SCRIPT, DEFAULT_ROOT_NAME, CONFIG_DEFAULT_FILENAME, ensure_root_dir
from . import __version__ as _VERSION
from . import ensure_config_default

# ─── Help text ────────────────────────────────────────────────

_HELP = textwrap.dedent(
    f"""
pyruns v{_VERSION}

USAGE
    pyr <script.py>                  Start UI for script (e.g., `pyr train.py`)
    pyr <script.py> [config.yaml]    Start UI and import a custom YAML config
    pyr help | version               Show help / version

EXAMPLES
    1. Zero Config (Argparse)
       Just run your existing argparse scripts. No code changes needed!
       $ pyr train.py

    2. Custom YAML Config
       $ pyr train.py my_config.yaml
       
       In your script, load the generated UI parameters like this:
       >>> import pyruns
       >>> config = pyruns.load()
       >>> print(config.learning_rate)
       
       For more examples, check out the `examples/` folder in the repository!
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

    # ── Dev mode: pyr dev <script.py> [custom.yaml] ──
    if arg == "dev":
        if len(sys.argv) < 3:
            print("Usage: pyr dev <script.py> [custom_config.yaml]")
            sys.exit(1)
        script_arg = sys.argv[2]
        custom_yaml = sys.argv[3] if len(sys.argv) > 3 else None
        _launch_dev(script_arg, custom_yaml)
        return

    # ── Resolve script path ──
    filepath = os.path.abspath(arg).replace("\\", "/")
    custom_yaml = sys.argv[2] if len(sys.argv) > 2 else None

    if not os.path.exists(filepath):
        print(f"Error: '{arg}' not found.")
        sys.exit(1)

    ensure_root_dir()

    try:
        _setup_env(filepath, custom_yaml)

        # ── Launch UI ──
        sys.argv = [sys.argv[0]]
        from pyruns.ui.app import main

        main()

    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)


def _setup_env(filepath: str, custom_yaml: str = None) -> str:
    """Detect config source, ensure workspace, set env vars.

    Returns the pyruns root directory path.
    """
    import shutil
    from pyruns.utils.parse_utils import (
        detect_config_source_fast,
        extract_argparse_params,
        generate_config_file,
        resolve_config_path,
    )
    from pyruns.utils.settings import ensure_settings_file

    file_dir = os.path.dirname(filepath).replace("\\", "/")
    script_base = os.path.splitext(os.path.basename(filepath))[0]
    
    global_pyruns_dir = os.path.join(file_dir, DEFAULT_ROOT_NAME).replace("\\", "/")
    pyruns_dir = os.path.join(global_pyruns_dir, script_base).replace("\\", "/")

    os.makedirs(pyruns_dir, exist_ok=True)
    ensure_settings_file(pyruns_dir)

    info_path = os.path.join(pyruns_dir, "script_info.json")
    if not os.path.exists(info_path):
        import time, json
        script_info = {
            "script_name": script_base,
            "script_path": filepath,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump(script_info, f, indent=4)

    config_default_path = os.path.join(pyruns_dir, CONFIG_DEFAULT_FILENAME)

    if custom_yaml:
        yaml_path = resolve_config_path(custom_yaml, file_dir)
        if yaml_path and os.path.exists(yaml_path):
            shutil.copy2(yaml_path, config_default_path)
            custom_yaml_clean = custom_yaml.replace("\\", "/")
            print(f"\n\033[92m[PYRUNS] Imported {custom_yaml_clean} as default config -> {DEFAULT_ROOT_NAME}/{script_base}/{CONFIG_DEFAULT_FILENAME}\033[0m\n")
        else:
            print(f"Error: Custom config '{custom_yaml}' not found.")
            sys.exit(1)
    else:
        # ── Detect config source ──
        mode, extra = detect_config_source_fast(filepath)

        if mode == "argparse":
            params = extract_argparse_params(filepath)
            generate_config_file(pyruns_dir, filepath, params)

        elif mode == "pyruns_read":
            if extra:
                config_file = resolve_config_path(extra, file_dir)

                if not config_file:
                    print(f"Error: Config '{extra}' not found.")
                    sys.exit(1)

        elif mode == "pyruns_load":
            if not os.path.exists(config_default_path):
                print(f"\033[91mError: Missing default config file for {script_base}.py\033[0m")
                print(f"When using pyruns.load(), you must either:")
                print(f"  1. Have a {CONFIG_DEFAULT_FILENAME} in your run root.")
                print(f"  2. Provide a yaml via CLI: `pyr {script_base}.py settings.yaml`")
                sys.exit(1)

        if mode != "pyruns_load":
            # Other modes like argparse or pyruns_read can safely initialize default blanks
            ensure_config_default(pyruns_dir)
    
    # ── Set environment ──
    os.environ[ENV_KEY_ROOT] = pyruns_dir
    os.environ[ENV_KEY_SCRIPT] = filepath
    return pyruns_dir


def _launch_dev(script_arg: str, custom_yaml: str = None):
    """Launch UI in dev mode with hot-reload via subprocess."""
    import subprocess as _sp

    filepath = os.path.abspath(script_arg)
    if not os.path.exists(filepath):
        print(f"Error: '{script_arg}' not found.")
        sys.exit(1)

    _setup_env(filepath, custom_yaml)
    print("[pyruns dev] Hot-reload enabled — editing .py files will auto-restart")
    print(f"[pyruns dev] Script: {filepath}")
    _sp.run([sys.executable, "-m", "pyruns.ui.app"])


if __name__ == "__main__":
    pyr()

