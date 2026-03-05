"""
CLI entry point — ``pyr <script.py>`` or ``pyr <command>``.

This package replaces the old single-file ``cli.py``.  The ``pyr()`` function
is the console_scripts entry point registered in ``pyproject.toml``.
"""

import os
import sys
import textwrap
import traceback

from pyruns._config import ENV_KEY_ROOT, ENV_KEY_SCRIPT, DEFAULT_ROOT_NAME, CONFIG_DEFAULT_FILENAME, ensure_root_dir
from pyruns import __version__ as _VERSION
from pyruns import ensure_config_default

# ── CLI sub-command names (checked before script-path resolution) ──
_CLI_COMMANDS = {"cli", "ls", "list", "gen", "generate", "run", "delete", "del", "rm", "jobs", "fg"}

# ─── Help text ────────────────────────────────────────────────

_HELP = textwrap.dedent(
    f"""
pyruns v{_VERSION}

USAGE
    pyr <script.py>                  Start UI for script (e.g., `pyr train.py`)
    pyr <script.py> [config.yaml]    Start UI and import a custom YAML config
    pyr dev <script.py>              Start UI in dev mode (hot-reload)
    pyr cli                          Enter interactive CLI mode
    pyr <command> [args]             Run a CLI command directly

CLI COMMANDS
    ls [query]            List tasks (with optional filter)
    gen [template]        Generate tasks from YAML config
    run <name|#>          Run task(s) by name or index
    delete <name|#>       Soft-delete task(s)
    jobs                  Show running/queued tasks
    fg <%N|name|#>        Tail a task's log (Ctrl+C to detach)

EXAMPLES
    pyr train.py                     Launch UI for train.py
    pyr ls                           List tasks in current workspace
    pyr run 1                        Run the first task
    pyr cli                          Enter interactive shell
    """.strip()
)


def _print_help():
    print(_HELP)
    sys.exit(0)


def _print_version():
    print(f"pyruns {_VERSION}")
    sys.exit(0)


# ─── Workspace resolution ────────────────────────────────────


def _resolve_workspace() -> str | None:
    """Auto-detect the ``_pyruns_/<script>`` workspace in the current directory.

    Walks up from CWD looking for a ``_pyruns_`` directory that contains at
    least one sub-directory with a ``script_info.json``.  Returns the first
    match (e.g. ``/project/_pyruns_/main``), or ``None``.
    """
    import json

    cwd = os.getcwd()
    pyruns_dir = os.path.join(cwd, DEFAULT_ROOT_NAME)

    if not os.path.isdir(pyruns_dir):
        return None

    # Find the first sub-directory with script_info.json
    for entry in sorted(os.listdir(pyruns_dir)):
        candidate = os.path.join(pyruns_dir, entry)
        info_path = os.path.join(candidate, "script_info.json")
        if os.path.isdir(candidate) and os.path.exists(info_path):
            return candidate

    return None


def _init_task_manager(workspace: str):
    """Create a TaskManager pointed at the workspace's tasks/ directory."""
    from pyruns._config import TASKS_DIR
    from pyruns.core.task_manager import TaskManager

    tasks_dir = os.path.join(workspace, TASKS_DIR)
    os.makedirs(tasks_dir, exist_ok=True)

    os.environ[ENV_KEY_ROOT] = workspace
    tm = TaskManager(root_dir=tasks_dir)

    # Wait for async scan to complete
    import time
    for _ in range(50):
        if tm.tasks is not None:
            break
        time.sleep(0.05)
    # Give scan a moment to finish
    time.sleep(0.2)
    tm.refresh_from_disk(force_all=True)
    return tm


# ─── CLI command dispatch ─────────────────────────────────────


def _dispatch_cli(args: list) -> None:
    """Handle CLI commands (both ``pyr cli`` and ``pyr <command>``)."""
    workspace = _resolve_workspace()
    if not workspace:
        print(f"No pyruns workspace found in current directory.")
        print(f"Hint: run `pyr <script.py>` first to initialize a workspace.")
        sys.exit(1)

    tm = _init_task_manager(workspace)

    if not args or args[0] == "cli":
        # Enter interactive REPL
        from pyruns.cli.interactive import run_interactive
        run_interactive(tm)
    else:
        from pyruns.cli.commands import COMMANDS
        cmd_name = args[0].lower()
        handler = COMMANDS.get(cmd_name)
        if handler:
            handler(tm, args[1:])
        else:
            print(f"Unknown command: '{cmd_name}'")
            print(f"Run 'pyr help' for available commands.")
            sys.exit(1)


# ─── Main entry ───────────────────────────────────────────────


def pyr():
    """``pyr`` — main console_scripts entry point."""

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

    # ── CLI commands ──
    if arg.lower() in _CLI_COMMANDS:
        _dispatch_cli(sys.argv[1:])
        return

    # ── Script mode: pyr <script.py> [config.yaml] ──
    filepath = os.path.abspath(arg).replace("\\", "/")
    custom_yaml = sys.argv[2] if len(sys.argv) > 2 else None

    if not os.path.exists(filepath):
        # Could be a typo of a CLI command
        print(f"Error: '{arg}' is not a file or known command.")
        print(f"Run 'pyr help' for usage.")
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


# ─── Environment setup (shared with dev mode) ────────────────


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

    pyruns_dir = os.path.join(file_dir, DEFAULT_ROOT_NAME).replace("\\", "/")
    script_dir = os.path.join(pyruns_dir, script_base).replace("\\", "/")

    os.makedirs(script_dir, exist_ok=True)
    ensure_settings_file(pyruns_dir)

    info_path = os.path.join(script_dir, "script_info.json")
    if not os.path.exists(info_path):
        import time, json
        script_info = {
            "script_name": script_base,
            "script_path": filepath,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump(script_info, f, indent=4)

    config_default_path = os.path.join(script_dir, CONFIG_DEFAULT_FILENAME)

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
            generate_config_file(script_dir, filepath, params)

        elif mode == "pyruns_load":
            if not os.path.exists(config_default_path):
                print(f"\033[91mError: Missing default config file for {script_base}.py\033[0m")
                print(f"When using pyruns.load(), you must either:")
                print(f"  1. Have a {CONFIG_DEFAULT_FILENAME} in your run root.")
                print(f"  2. Provide a yaml via CLI: `pyr {script_base}.py settings.yaml`")
                sys.exit(1)

        if mode != "pyruns_load":
            # Other modes like argparse/unknown can safely initialize default blanks
            ensure_config_default(script_dir)

    # ── Set environment ──
    os.environ[ENV_KEY_ROOT] = script_dir
    os.environ[ENV_KEY_SCRIPT] = filepath
    return script_dir


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
