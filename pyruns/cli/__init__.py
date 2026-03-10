"""
CLI entry point 鈥?``pyr <script.py>`` or ``pyr <command>``.

This package replaces the old single-file ``cli.py``.  The ``pyr()`` function
is the console_scripts entry point registered in ``pyproject.toml``.
"""

import os
import sys
import textwrap
import traceback

from pyruns._config import ENV_KEY_ROOT, DEFAULT_ROOT_NAME, CONFIG_DEFAULT_FILENAME, ensure_root_dir
from pyruns import __version__ as _VERSION
from pyruns import ensure_config_default

from pyruns.utils import get_logger
logger = get_logger(__name__)

# 鈹€鈹€ CLI sub-command names (checked before script-path resolution) 鈹€鈹€
_CLI_COMMANDS = {"cli", "ls", "list", "gen", "generate", "run", "delete", "del", "rm", "jobs", "log", "fg"}

# 鈹€鈹€鈹€ Help text 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

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
    gen --args [template] Generate tasks in args mode (args: "...")
    run <name|#>          Run task(s) by name or index
    delete <name|#>       Soft-delete task(s)
    jobs                  Show running/queued tasks
    log <%N|name|#>       View a task's log in alt-screen viewer
    fg <%N|name|#>        Tail a task's log inline (Ctrl+C to detach)

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



# 鈹€鈹€鈹€ Workspace resolution 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def _resolve_workspace(script_path: str = None) -> "str | None":
    """Auto-detect the ``_pyruns_/<script>`` workspace in the current directory.

    If script_path is provided, return that specific workspace.
    Otherwise, auto-detect by finding the script_info.json pointing
    to the most recently modified script file.
    """
    import json

    cwd = os.getcwd()
    pyruns_dir = os.path.join(cwd, DEFAULT_ROOT_NAME)

    if not os.path.isdir(pyruns_dir):
        return None

    if script_path:
        target_base = os.path.splitext(os.path.basename(script_path))[0]
        candidate = os.path.join(pyruns_dir, target_base)
        if os.path.isdir(candidate):
            return candidate
            
        target_abs = os.path.abspath(script_path).replace("\\", "/")
        for entry in os.listdir(pyruns_dir):
            candidate = os.path.join(pyruns_dir, entry)
            script_info_path = os.path.join(candidate, "script_info.json")
            if os.path.isdir(candidate) and os.path.exists(script_info_path):
                try:
                    with open(script_info_path, "r", encoding="utf-8") as f:
                        info = json.load(f)
                    if info.get("script_name") == target_base or info.get("script_path") == target_abs:
                        return candidate
                except Exception:
                    pass
        return None

    best_candidate = None
    latest_time = -1

    for entry in os.listdir(pyruns_dir):
        candidate = os.path.join(pyruns_dir, entry)
        script_info_path = os.path.join(candidate, "script_info.json")
        if os.path.isdir(candidate) and os.path.exists(script_info_path):
            try:
                with open(script_info_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
                s_path = info.get("script_path")
                if s_path and os.path.exists(s_path):
                    mtime = os.path.getmtime(s_path)
                    if mtime > latest_time:
                        latest_time = mtime
                        best_candidate = candidate
                elif not best_candidate:
                    best_candidate = candidate
            except Exception:
                if not best_candidate:
                    best_candidate = candidate

    return best_candidate


def _init_task_manager(workspace: str):
    """Create a TaskManager pointed at the workspace's tasks/ directory."""
    from pyruns._config import TASKS_DIR
    from pyruns.core.task_manager import TaskManager
    from pyruns.utils.settings import ensure_settings_file, load_settings

    tasks_dir = os.path.join(workspace, TASKS_DIR)
    os.makedirs(tasks_dir, exist_ok=True)

    os.environ[ENV_KEY_ROOT] = workspace
    ensure_settings_file(workspace)
    load_settings(workspace)
    
    tm = TaskManager(tasks_dir=tasks_dir, lazy_scan=False)
    logger.debug(f"vars: {vars(tm)}")
    return tm


# 鈹€鈹€鈹€ CLI command dispatch 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def _dispatch_cli(args: list) -> None:
    """Handle CLI commands (both ``pyr cli`` and ``pyr <command>``)."""
    
    # If the user ran `pyr cli script.py`, extract the script path
    script_path = None
    if args and len(args) > 1 and args[1].endswith('.py') and os.path.exists(args[1]):
        script_path = args[1]
        args = [args[0]] + args[2:]  # 鍓ョ鑴氭湰璺緞锛屼繚鐣欏叾浣欏懡浠ゅ弬鏁?
        
    if script_path:
        # 鏍稿績锛氬鐢ㄧ幆澧冩瀯寤洪€昏緫锛岃 CLI 涓?UI 琛屽姩瀹屽叏涓€鑷?
        workspace = _setup_env(script_path)
    else:
        # 浠呭綋鏈寚瀹氱壒瀹氳剼鏈椂锛屽皾璇曡嚜鍔ㄦ帹鏂綋鍓嶅伐浣滃尯
        workspace = _resolve_workspace()

    logger.debug(f"workspace={workspace}")

    if not workspace:
        print(f"No pyruns workspace found in current directory.")
        print(f"Hint: run `pyr ui <script.py>` first to initialize a workspace.")
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


# 鈹€鈹€鈹€ Main entry 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def pyr():
    """``pyr`` 鈥?main console_scripts entry point."""

    argv = list(sys.argv[1:])
    if not argv:
        argv = ["ui"]

    arg = argv[0]

    # Help / version flags
    if arg in ("help", "-h", "--help"):
        _print_help()
    if arg in ("version", "-v", "--version"):
        _print_version()

    # 鈹€鈹€ Dev mode: pyr dev <script.py> [custom.yaml] 鈹€鈹€
    if arg == "dev":
        if len(argv) < 2:
            print("Usage: pyr dev <script.py> [custom_config.yaml]")
            sys.exit(1)
        script_arg = argv[1]
        custom_yaml = argv[2] if len(argv) > 2 else None
        _launch_dev(script_arg, custom_yaml)
        return

    # 鈹€鈹€ CLI commands 鈹€鈹€
    if arg.lower() in _CLI_COMMANDS:
        _dispatch_cli(argv)
        return

    # 鈹€鈹€ UI Mode (explicit `ui` or implicit `<script.py>`) 鈹€鈹€
    if arg == "ui":
        if len(argv) > 1:
            filepath = argv[1]
            custom_yaml = argv[2] if len(argv) > 2 else None
        else:
            # Auto-detect latest script
            ws = _resolve_workspace()
            if not ws:
                print("Error: No pyruns workspace found to auto-detect a script.")
                sys.exit(1)
            # Read script path from workspace
            script_info_path = os.path.join(ws, "script_info.json")
            import json
            filepath = None
            if os.path.exists(script_info_path):
                with open(script_info_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
                filepath = info.get("script_path")
            # Fallback: derive from workspace name (e.g. _pyruns_/main -> main.py)
            if not filepath or not os.path.exists(filepath):
                project_root = os.path.dirname(os.path.dirname(ws))
                ws_name = os.path.basename(ws)
                for ext in (".py", ""):
                    candidate = os.path.join(project_root, ws_name + ext)
                    if os.path.exists(candidate):
                        filepath = candidate
                        break
            if not filepath or not os.path.exists(filepath):
                print("Error: Cannot find the original script for the workspace.")
                print(f"  Workspace: {ws}")
                print(f"  Try: pyr ui <script.py>")
                sys.exit(1)
            custom_yaml = None
    else:
        # Implicit UI mode: pyr <script.py>
        filepath = arg
        custom_yaml = argv[1] if len(argv) > 1 else None

    filepath = os.path.abspath(filepath).replace("\\", "/")

    if not os.path.exists(filepath):
        # Could be a typo of a CLI command
        print(f"Error: '{arg}' is not a file or known command.")
        print(f"Run 'pyr help' for usage.")
        sys.exit(1)

    ensure_root_dir()

    try:
        _setup_env(filepath, custom_yaml)

        # 鈹€鈹€ Launch UI 鈹€鈹€
        sys.argv = [sys.argv[0]]
        from pyruns.ui.app import main

        main()

    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)


# 鈹€鈹€鈹€ Environment setup (shared with dev mode) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


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

    filepath = os.path.abspath(filepath).replace("\\", "/")
    file_dir = os.path.dirname(filepath).replace("\\", "/")
    script_base = os.path.splitext(os.path.basename(filepath))[0]

    pyruns_dir = os.path.join(file_dir, DEFAULT_ROOT_NAME).replace("\\", "/")
    script_dir = os.path.join(pyruns_dir, script_base).replace("\\", "/")

    os.makedirs(script_dir, exist_ok=True)
    ensure_settings_file(pyruns_dir)

    script_info_path = os.path.join(script_dir, "script_info.json")
    if not os.path.exists(script_info_path):
        import time, json
        script_info = {
            "script_name": script_base,
            "script_path": filepath,  # Saved as absolute path
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        with open(script_info_path, "w", encoding="utf-8") as f:
            json.dump(script_info, f, indent=4)

    config_default_path = os.path.join(script_dir, CONFIG_DEFAULT_FILENAME)

    mode, _ = detect_config_source_fast(filepath)

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

        if mode == "argparse":
            # argparse mode always has a generated config_default.yaml
            ensure_config_default(script_dir)

    # 鈹€鈹€ Set environment 鈹€鈹€
    os.environ[ENV_KEY_ROOT] = script_dir
    return script_dir


def _launch_dev(script_arg: str, custom_yaml: str = None):
    """Launch UI in dev mode with hot-reload via subprocess."""
    import subprocess as _sp

    filepath = os.path.abspath(script_arg)
    if not os.path.exists(filepath):
        print(f"Error: '{script_arg}' not found.")
        sys.exit(1)

    _setup_env(filepath, custom_yaml)
    print("[pyruns dev] Hot-reload enabled 鈥?editing .py files will auto-restart")
    print(f"[pyruns dev] Script: {filepath}")
    _sp.run([sys.executable, "-m", "pyruns.ui.app"])


if __name__ == "__main__":
    pyr()

