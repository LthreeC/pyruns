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
from pyruns.cli.commands import COMMANDS
from pyruns._config import (
    DEFAULT_ROOT_NAME,
    ENV_KEY_CLI_TERMINAL_RUNTIME,
    ENV_KEY_ROOT,
    SCRIPT_INFO_FILENAME,
    SHELL_WORKSPACE_NAME,
    ensure_root_dir,
)
from pyruns.launcher import (
    bootstrap_from_cli,
    bootstrap_shell_workspace,
    launcher_query,
    normalize_path,
)
from pyruns.utils import get_logger
from pyruns.utils.parse_utils import resolve_config_path

logger = get_logger(__name__)

_CLI_COMMANDS = frozenset({"cli", *COMMANDS.keys()})
_YAML_EXTENSIONS = (".yaml", ".yml")

_HELP = textwrap.dedent(
    f"""
    pyruns v{_VERSION}

    USAGE
        pyr <script.py>                Start web app for a script
        pyr <script.py> [config.yaml]  Start web app and import a custom YAML config
        pyr                            Start web app in shell mode for current directory
        pyr -p <port>                  Start web app on a custom port
        pyr --no-browser               Start web app without opening a browser
        pyr ui                         Start the launcher and choose a script workspace
        pyr dev <script.py>            Start web app in dev mode (hot-reload)
        pyr cli [script.py]            Enter interactive CLI mode
        pyr run <script.py> <config>   Create and run YAML task(s) without UI
        pyr run <script.py> <task>     Run an existing task without UI
        pyr <command> [args]           Run a CLI command directly

    CLI COMMANDS
        ls [query]                    List tasks (supports --status, --limit, -i)
        show <name|#>                 Show detailed task info
        gen [template]                Generate tasks from YAML config
        run <name|# ...>              Run task(s); multi-task supports --workers/--mode
        delete <name|# ...> [-y]      Soft-delete task(s)
        open <name|#> [config|task]   Open config.yaml or task_info.json in editor
        export [targets]              Export task data (csv/json)
        jobs                          Show running/queued tasks
        stat [-i]                     Show system metrics
        info                          Show current workspace info
        log <name|#>                  View a task's log in alt-screen viewer
        fg <name|#>                   Tail a task's log inline (Ctrl+C to detach)

    EXAMPLES
        pyr train.py
        pyr train.py -p 9000
        pyr train.py -p 9000 --no-browser
        pyr -p 9000
        pyr --no-browser
        pyr train.py settings.yaml
        pyr
        pyr ui
        pyr cli train.py
        pyr run train.py configs/quick.yaml
        pyr run train.py baseline_task
        pyr ls
        pyr ls --status completed --limit 20
        pyr show 1
        pyr run 1
        pyr run 1 2 3 --workers 3 --detach
        pyr export --format json

    NOTES
        CLI task runs inherit the current terminal environment.
        Web UI Runtime and Workspace Env settings apply to UI-launched runs.
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
            if entry == SHELL_WORKSPACE_NAME:
                continue
            workspace = os.path.join(pyruns_dir, entry)
            script_info_path = os.path.join(workspace, SCRIPT_INFO_FILENAME)
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
        if entry == SHELL_WORKSPACE_NAME:
            continue
        workspace = os.path.join(pyruns_dir, entry)
        script_info_path = os.path.join(workspace, SCRIPT_INFO_FILENAME)
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


def _init_task_manager(workspace: str, *, lazy_scan: bool | None = False):
    """Create a TaskManager pointed at the workspace's tasks directory."""

    from pyruns._config import TASKS_DIR
    from pyruns.core.task_manager import TaskManager
    from pyruns.utils.settings import ensure_settings_file, load_settings

    tasks_dir = os.path.join(workspace, TASKS_DIR)
    os.makedirs(tasks_dir, exist_ok=True)
    os.environ[ENV_KEY_ROOT] = workspace
    ensure_settings_file(workspace)
    load_settings(workspace)
    task_manager = TaskManager(tasks_dir=tasks_dir, lazy_scan=lazy_scan)
    logger.debug("task manager ready: %s", vars(task_manager))
    return task_manager


def _is_yaml_arg(arg: str) -> bool:
    return str(arg or "").lower().endswith(_YAML_EXTENSIONS)


def _resolve_script_yaml_arg(arg: str, script_path: str | None) -> str | None:
    if not _is_yaml_arg(arg):
        return None
    script_dir = os.path.dirname(os.path.abspath(script_path)) if script_path else os.getcwd()
    return resolve_config_path(arg, script_dir)


def _dispatch_cli(args: list[str]) -> None:
    """Handle direct CLI commands and interactive CLI mode."""

    os.environ[ENV_KEY_CLI_TERMINAL_RUNTIME] = "1"

    script_path = None
    script_cli_args: list[str] = []
    if len(args) > 1 and args[1].endswith(".py") and os.path.exists(args[1]):
        script_path = args[1]
        script_cli_args = args[2:]
        args = [args[0], *args[2:]]

    command_name = args[0].lower() if args else ""
    run_config_path = (
        _resolve_script_yaml_arg(script_cli_args[0], script_path)
        if command_name == "run" and script_path and script_cli_args
        else None
    )
    custom_yaml = (
        script_cli_args[0]
        if run_config_path
        else None
    )
    if script_path and custom_yaml:
        workspace = _setup_env(script_path, custom_yaml)
    elif script_path:
        workspace = _setup_env(script_path)
    else:
        workspace = _resolve_workspace()
    logger.debug("workspace=%s", workspace)
    if not workspace:
        print("No pyruns workspace found in current directory.")
        print("Hint: run `pyr` to open a shell workspace, or `pyr <script.py>` to open a script workspace first.")
        sys.exit(1)

    defer_initial_scan = bool(args and args[0].lower() == "run")
    task_manager = _init_task_manager(workspace, lazy_scan=None if defer_initial_scan else False)
    if script_path and args and args[0].lower() == "run" and len(args) > 1 and run_config_path:
        from pyruns.cli.commands import cmd_run_config

        cmd_run_config(task_manager, args[1], args[2:], script_path=script_path)
        return

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


def _parse_port_value(raw: str) -> int:
    """Parse and validate a TCP port for UI startup."""

    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        print(f"Invalid port: {raw}")
        sys.exit(1)
    if value < 1 or value > 65535:
        print("Port must be between 1 and 65535.")
        sys.exit(1)
    return value


def _consume_ui_options(args: list[str]) -> tuple[int | None, bool | None, list[str]]:
    """Remove UI launch options from args and return the selected values."""

    selected_port: int | None = None
    open_browser: bool | None = None
    remaining: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-p", "--port"}:
            if index + 1 >= len(args):
                print(f"Missing value for {arg}.")
                sys.exit(1)
            selected_port = _parse_port_value(args[index + 1])
            index += 2
            continue
        if arg.startswith("--port="):
            selected_port = _parse_port_value(arg.split("=", 1)[1])
            index += 1
            continue
        if arg == "--no-browser":
            open_browser = False
            index += 1
            continue
        if arg in {"--browser", "--open-browser"}:
            open_browser = True
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return selected_port, open_browser, remaining


def _has_ui_launch_option(args: list[str]) -> bool:
    """Return whether *args* contain options that only make sense for UI launch."""

    for arg in args:
        if arg in {"-p", "--port", "--no-browser", "--browser", "--open-browser"}:
            return True
        if arg.startswith("--port="):
            return True
    return False


def _launch_ui(start_path: str = "/", *, port: int | None = None, open_browser: bool | None = None) -> None:
    """Launch the unified static UI and API server."""

    sys.argv = [sys.argv[0]]
    from pyruns.web.app import main

    main(start_path=start_path, port=port, open_browser=open_browser)


def _handle_ui_launch(
    filepath: str,
    custom_yaml: str | None,
    *,
    port: int | None = None,
    open_browser: bool | None = None,
) -> None:
    """Launch the UI for a given script path."""

    normalized = normalize_path(filepath)
    if not os.path.exists(normalized):
        print(f"Error: '{filepath}' is not a file or known command.")
        print("Run 'pyr help' for usage.")
        sys.exit(1)

    ensure_root_dir()
    _setup_env(normalized, custom_yaml)
    _launch_ui("/", port=port, open_browser=open_browser)


def _launch_shell_workspace_ui(*, port: int | None = None, open_browser: bool | None = None) -> None:
    """Launch the web UI directly into the current directory's shell workspace."""

    root_dir = normalize_path(os.path.join(os.getcwd(), DEFAULT_ROOT_NAME))
    ensure_root_dir(root_dir)
    shell_root = bootstrap_shell_workspace(root_dir)
    print("[pyruns] Starting shell workspace for current directory")
    print(f"[pyruns] Workspace: {shell_root}")
    print("[pyruns] Recommended main flow: `pyr <script.py>` or `pyr <script.py> <config.yaml>`")
    print("[pyruns] Tip: choose a script in Launcher, or cancel it and write commands in Shell Generator mode")
    _launch_ui("/generator?launcher=1", port=port, open_browser=open_browser)


def pyr() -> None:
    """Main ``pyr`` console entry point."""

    raw_argv = list(sys.argv[1:])
    if raw_argv and raw_argv[0].lower() in _CLI_COMMANDS:
        if _has_ui_launch_option(raw_argv[1:]):
            print("UI launch options only apply to UI launch commands.")
            sys.exit(1)
        _dispatch_cli(raw_argv)
        return

    port, open_browser, argv = _consume_ui_options(raw_argv)
    if not argv:
        _launch_shell_workspace_ui(port=port, open_browser=open_browser)
        return

    arg = argv[0]

    if arg in ("help", "-h", "--help"):
        _print_help()
    if arg in ("version", "-v", "--version"):
        _print_version()

    if arg == "dev":
        if len(argv) < 2:
            print("Usage: pyr dev <script.py> [custom_config.yaml]")
            sys.exit(1)
        _launch_dev(argv[1], argv[2] if len(argv) > 2 else None, port=port, open_browser=open_browser)
        return

    if arg.lower() in _CLI_COMMANDS:
        if port is not None or open_browser is not None:
            print("UI launch options only apply to UI launch commands.")
            sys.exit(1)
        _dispatch_cli(argv)
        return

    if arg == "ui" and len(argv) == 1:
        ensure_root_dir()
        print("[pyruns] Opening launcher")
        print("[pyruns] Tip: choose a Python script to enter script workspace mode")
        _launch_ui(launcher_query(), port=port, open_browser=open_browser)
        return

    if arg == "ui":
        _handle_ui_launch(argv[1], argv[2] if len(argv) > 2 else None, port=port, open_browser=open_browser)
        return

    _handle_ui_launch(arg, argv[1] if len(argv) > 1 else None, port=port, open_browser=open_browser)


def _setup_env(filepath: str, custom_yaml: str | None = None) -> str:
    """Prepare a workspace and return its root directory."""

    return bootstrap_from_cli(filepath, custom_yaml)


def _launch_dev(
    script_arg: str,
    custom_yaml: str | None = None,
    *,
    port: int | None = None,
    open_browser: bool | None = None,
) -> None:
    """Launch the unified web app in dev mode with hot-reload."""

    filepath = normalize_path(script_arg)
    if not os.path.exists(filepath):
        print(f"Error: '{script_arg}' not found.")
        sys.exit(1)

    _setup_env(filepath, custom_yaml)
    print("[pyruns dev] Hot-reload enabled - editing .py files will auto-restart")
    print(f"[pyruns dev] Script: {filepath}")
    command = [sys.executable, "-m", "pyruns.web.app"]
    if port is not None:
        command.extend(["--port", str(port)])
    if open_browser is False:
        command.append("--no-browser")
    elif open_browser is True:
        command.append("--browser")
    subprocess.run(command, check=False)


if __name__ == "__main__":
    pyr()
