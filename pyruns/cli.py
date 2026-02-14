"""
CLI entry point — ``pyr <script.py>`` or ``pyr help``.
"""
import os
import sys
import textwrap
import traceback

from ._config import ENV_ROOT, ENV_SCRIPT, DEFAULT_ROOT_NAME, CONFIG_DEFAULT_FILENAME
from . import __version__ as _VERSION

# ─── Help text ────────────────────────────────────────────────

_HELP = textwrap.dedent(f"""\
    pyruns v{_VERSION} — lightweight Python experiment management UI

    USAGE
      pyr <script.py>            Launch web UI for the given script
      pyr help | -h | --help     Show this message
      pyr version | -v           Show version

    ZERO CONFIG (argparse)
      If your script uses argparse, pyr extracts parameters
      automatically — no extra setup needed:

        $ pyr train.py           # that's it!

    MANUAL CONFIG (pyruns)
      Place a config file next to your script:

        your_project/
        └── {DEFAULT_ROOT_NAME}/
            └── {CONFIG_DEFAULT_FILENAME}   ← your parameters

      Then in your script:
        import pyruns
        config = pyruns.load()   # auto-reads config under pyr
        print(config.lr)

    API MODES
      • pyr mode:    config = pyruns.load()           # just works
      • manual mode: pyruns.read("path/to/cfg.yaml")  # explicit path
                     config = pyruns.load()

    WORKSPACE SETTINGS
      On first launch, pyr creates  {DEFAULT_ROOT_NAME}/_pyruns_.yaml
      Edit it to customise: UI port, refresh intervals, grid columns,
      default workers, execution mode, etc.

    WORKSPACE LAYOUT
      your_project/
      ├── train.py
      └── {DEFAULT_ROOT_NAME}/
          ├── _pyruns_.yaml          ← UI settings (auto-generated)
          ├── {CONFIG_DEFAULT_FILENAME}    ← parameter template
          ├── my-task/               ← generated task folder
          │   ├── task_info.json
          │   ├── config.yaml
          │   └── run.log
          └── .trash/                ← soft-deleted tasks
""")


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

    # ── Resolve script path ──
    filepath = os.path.abspath(arg)
    if not os.path.exists(filepath):
        print(f"Error: '{arg}' not found.")
        sys.exit(1)

    try:
        from pyruns.utils.parse_utils import (
            detect_config_source_fast,
            extract_argparse_params,
            generate_config_file,
            resolve_config_path,
        )
        from pyruns.utils.settings import ensure_settings_file

        # ── Detect config source ──
        mode, extra = detect_config_source_fast(filepath)

        file_dir = os.path.dirname(filepath)
        pyruns_dir = os.path.join(file_dir, DEFAULT_ROOT_NAME)

        if mode == "argparse":
            # Auto-extract params → generate config_default.yaml
            params = extract_argparse_params(filepath)
            generate_config_file(filepath, params)

        elif mode == "pyruns_read":
            # pyruns.read("path") — resolve the explicit config
            if extra:
                config_file = resolve_config_path(extra, file_dir)
                if not config_file:
                    print(f"Error: Config '{extra}' not found.")
                    sys.exit(1)
            # pyruns.read() with no arg → will use config_default.yaml at runtime

        # mode == "pyruns_load" or "unknown":
        #   No special handling needed — pyruns.load() auto-reads
        #   config_default.yaml when ENV_CONFIG is set by the executor.

        # Ensure workspace directory exists
        os.makedirs(pyruns_dir, exist_ok=True)

        # Auto-generate _pyruns_.yaml settings if absent
        ensure_settings_file(pyruns_dir)

        # ── Set environment ──
        os.environ[ENV_ROOT] = pyruns_dir
        os.environ[ENV_SCRIPT] = filepath

        # ── Launch UI ──
        sys.argv = [sys.argv[0]]
        from pyruns.ui.app import main
        main()

    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    pyr()
