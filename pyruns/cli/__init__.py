"""
CLI entry point for ``pyr`` commands and UI launch flows.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import traceback

from pyruns import __version__ as _VERSION
from pyruns._config import DEFAULT_ROOT_NAME, ENV_KEY_ROOT, ensure_root_dir
from pyruns.launcher import bootstrap_from_cli, launcher_query, list_config_candidates, normalize_path
from pyruns.utils import get_logger

logger = get_logger(__name__)

_CLI_COMMANDS = {"cli", "ls", "list", "gen", "generate", "run", "delete", "del", "rm", "jobs", "log", "fg"}

_HELP = textwrap.dedent(
    f"""
    pyruns v{_VERSION}

    USAGE
        pyr                            Start the launcher and choose a workspace
        pyr <script.py>                Start web app for a script
        pyr <script.py> [config.yaml]  Start web app and import a custom YAML config
        pyr dev <script.py>            Start web app in dev mode (hot-reload)
        pyr cli                        Enter interactive CLI mode
        pyr <command> [args]           Run a CLI command directly

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
        pyr
        pyr train.py
        pyr train.py settings.yaml
        pyr ls
        pyr run 1
        pyr cli
    """.strip()
)


def _print_help() -> None:
    print(_HELP)
    sys.exit(0)


def _print_version() -> None:
    print(f"pyruns {_VERSION}")
    sys.exit(0)


def _resolve_workspace(script_path: str | None = None) -> str | None:
    """Auto-detect an existing workspace from the current directory."""

    cwd = os.getcwd()
    pyruns_dir = os.path.join(cwd, DEFAULT_ROOT_NAME)
    if not os.path.isdir(pyruns_dir):
        return None

    if script_path:
        target_base = os.path.splitext(os.path.basename(script_path))[0]
        candidate = os.path.join(pyruns_dir, target_base)
        if os.path.isdir(candidate):
            return candidate

        target_abs = normalize_path(script_path)
        for entry in os.listdir(pyruns_dir):
            workspace = os.path.join(pyruns_dir, entry)
            script_info_path = os.path.join(workspace, "script_info.json")
            if not os.path.isfile(script_info_path):
                continue
            try:
                with open(script_info_path, "r", encoding="utf-8") as handle:
                    info = json.load(handle)
            except Exception:
                continue
            if info.get("script_name") == target_base or normalize_path(str(info.get("script_path", ""))) == target_abs:
                return workspace
        return None

    best_candidate = None
    latest_time = -1.0
    for entry in os.listdir(pyruns_dir):
        workspace = os.path.join(pyruns_dir, entry)
        script_info_path = os.path.join(workspace, "script_info.json")
        if not os.path.isfile(script_info_path):
            continue
        try:
            with open(script_info_path, "r", encoding="utf-8") as handle:
                info = json.load(handle)
            script_file = str(info.get("script_path", "") or "")
            if script_file and os.path.exists(script_file):
                modified = os.path.getmtime(script_file)
                if modified > latest_time:
                    latest_time = modified
                    best_candidate = workspace
            elif best_candidate is None:
                best_candidate = workspace
        except Exception:
            if best_candidate is None:
                best_candidate = workspace
    return best_candidate


def _init_task_manager(workspace: str):
    """Create a TaskManager pointed at the workspace's tasks directory."""

    from pyruns._config import TASKS_DIR
    from pyruns.core.task_manager import TaskManager
    from pyruns.utils.settings import ensure_settings_file, load_settings

    tasks_dir = os.path.join(workspace, TASKS_DIR)
    os.makedirs(tasks_dir, exist_ok=True)
    os.environ[ENV_KEY_ROOT] = workspace
    ensure_settings_file(workspace)
    load_settings(workspace)
    task_manager = TaskManager(tasks_dir=tasks_dir, lazy_scan=False)
    logger.debug("task manager ready: %s", vars(task_manager))
    return task_manager


def _dispatch_cli(args: list[str]) -> None:
    """Handle direct CLI commands and interactive CLI mode."""

    script_path = None
    if len(args) > 1 and args[1].endswith(".py") and os.path.exists(args[1]):
        script_path = args[1]
        args = [args[0], *args[2:]]

    workspace = _setup_env(script_path) if script_path else _resolve_workspace()
    logger.debug("workspace=%s", workspace)
    if not workspace:
        print("No pyruns workspace found in current directory.")
        print("Hint: run `pyr` or `pyr <script.py>` first to initialize a workspace.")
        sys.exit(1)

    task_manager = _init_task_manager(workspace)
    if not args or args[0] == "cli":
        from pyruns.cli.interactive import run_interactive

        run_interactive(task_manager)
        return

    from pyruns.cli.commands import COMMANDS

    cmd_name = args[0].lower()
    handler = COMMANDS.get(cmd_name)
    if handler is None:
        print(f"Unknown command: '{cmd_name}'")
        print("Run 'pyr help' for available commands.")
        sys.exit(1)
    handler(task_manager, args[1:])


def _launch_ui(start_path: str = "/") -> None:
    """Launch the unified static UI and API server."""

    sys.argv = [sys.argv[0]]
    from pyruns.web.app import main

    main(start_path=start_path)


def _handle_ui_launch(filepath: str, custom_yaml: str | None) -> None:
    """Launch the UI for a given script path."""

    normalized = normalize_path(filepath)
    if not os.path.exists(normalized):
        print(f"Error: '{filepath}' is not a file or known command.")
        print("Run 'pyr help' for usage.")
        sys.exit(1)

    ensure_root_dir()
    start_path = "/"

    if custom_yaml:
        _setup_env(normalized, custom_yaml)
    else:
        configs = list_config_candidates(normalized)
        selectable = [item for item in configs if item.get("kind") != "workspace_default"]
        if len(selectable) > 1:
            os.environ.pop(ENV_KEY_ROOT, None)
            start_path = launcher_query(normalized)
        elif len(selectable) == 1:
            _setup_env(normalized, selectable[0]["path"])
        else:
            _setup_env(normalized)

    _launch_ui(start_path)


def pyr() -> None:
    """Main ``pyr`` console entry point."""

    argv = list(sys.argv[1:]) or ["ui"]
    arg = argv[0]

    if arg in ("help", "-h", "--help"):
        _print_help()
    if arg in ("version", "-v", "--version"):
        _print_version()

    if arg == "dev":
        if len(argv) < 2:
            print("Usage: pyr dev <script.py> [custom_config.yaml]")
            sys.exit(1)
        _launch_dev(argv[1], argv[2] if len(argv) > 2 else None)
        return

    if arg.lower() in _CLI_COMMANDS:
        _dispatch_cli(argv)
        return

    if arg == "ui" and len(argv) == 1:
        ensure_root_dir()
        _launch_ui(launcher_query())
        return

    if arg == "ui":
        _handle_ui_launch(argv[1], argv[2] if len(argv) > 2 else None)
        return

    _handle_ui_launch(arg, argv[1] if len(argv) > 1 else None)


def _setup_env(filepath: str, custom_yaml: str | None = None) -> str:
    """Prepare a workspace and return its root directory."""

    return bootstrap_from_cli(filepath, custom_yaml)


def _launch_dev(script_arg: str, custom_yaml: str | None = None) -> None:
    """Launch the unified web app in dev mode with hot-reload."""

    filepath = normalize_path(script_arg)
    if not os.path.exists(filepath):
        print(f"Error: '{script_arg}' not found.")
        sys.exit(1)

    _setup_env(filepath, custom_yaml)
    print("[pyruns dev] Hot-reload enabled - editing .py files will auto-restart")
    print(f"[pyruns dev] Script: {filepath}")
    subprocess.run([sys.executable, "-m", "pyruns.web.app"], check=False)


if __name__ == "__main__":
    pyr()
