"""Declarative, script-friendly command line interface for Pyruns."""

from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass
from typing import Sequence

from pyruns import __version__


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keep examples readable while retaining argparse's option formatting."""

    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=30, width=100)


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser with concise, consistent errors."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


@dataclass(frozen=True)
class CliContext:
    """Global CLI options shared by every command."""

    directory: str
    workspace: str | None
    json_output: bool
    debug: bool
    program: str = "pyr"


def _invoked_program(argv0: str | None = None) -> str:
    """Return the official executable name used for this invocation."""

    stem = os.path.splitext(os.path.basename(argv0 or sys.argv[0]))[0].lower()
    if stem.startswith("pyruns"):
        return "pyruns"
    if stem.startswith("pyr"):
        return "pyr"
    return "pyr"


def _example_block(program: str, *examples: str) -> str:
    return "Examples:\n" + "\n".join(f"  {program} {example}" for example in examples)


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid port: {value}") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive integer: {value}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a non-negative number: {value}") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("value must be a finite number that is zero or greater")
    return parsed


def _add_browser_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-p", "--port", type=_port, help="TCP port for the web application")
    browser = parser.add_mutually_exclusive_group()
    browser.add_argument("--browser", action="store_true", help="open the browser after startup")
    browser.add_argument("--no-browser", action="store_true", help="do not open a browser")


def build_parser(
    program: str = "pyr",
    *,
    show_all_commands: bool = False,
) -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]:
    """Build the complete CLI parser and its command-help index."""

    alternate = "pyruns" if program == "pyr" else "pyr"
    command_overview = (
        "All command groups:\n"
        "  create/run       init  exec  add  run\n"
        "  inspect          ls  status  show  log\n"
        "  wait/control     wait  stop  rm  restore  mv  pin\n"
        "  output/settings  export  config  metrics\n"
        "  optional Web UI  ui  dev\n\n"
        if show_all_commands
        else ""
    )
    parser = _ArgumentParser(
        prog=program,
        formatter_class=_HelpFormatter,
        description=(
            "Pyruns records reproducible terminal commands and Python experiments under _pyruns_.\n"
            f"{program} and {alternate} are identical; every command is one-shot and script-friendly.\n\n"
            "Quick start:\n"
            "  Track any terminal command (no setup required):\n"
            f"    {program} exec -n check -- python -V\n\n"
            "  Run a shell script with the interpreter selected from its extension:\n"
            f"    {program} exec -n setup -- ./setup.sh\n\n"
            "  Run a Python experiment from YAML:\n"
            f"    {program} init train.py\n"
            f"    {program} -w train add configs/quick.yaml -n quick\n"
            f"    {program} -w train run quick"
        ),
        epilog=(
            command_overview + "Workspace selection:\n"
            f"  {program} exec ... uses the shell workspace and creates it when needed.\n"
            "  Task commands use the only nearby workspace. With several, select one with\n"
            "  -w shell, -w NAME, -w PATH, or -w SCRIPT.py.\n"
            f"  The Web UI uses '{program} ui [WORKSPACE|SCRIPT.py]' instead of -w.\n"
            "  Global options such as -C, -w, and --json must appear before COMMAND.\n\n"
            "More help:\n"
            f"  {program} help COMMAND\n"
            f"  {program} help -a        list every command\n"
            f"  {program} COMMAND --help\n\n"
            "Exit status: 0 success, 1 operation failed, 2 invalid usage, 130 interrupted."
        ),
    )
    parser.add_argument(
        "-C",
        "--directory",
        default=".",
        metavar="PATH",
        help="run as if Pyruns was started in PATH",
    )
    parser.add_argument(
        "-w",
        "--workspace",
        metavar="NAME|PATH|SCRIPT.py",
        help="select an exact task workspace; use 'shell' for the shell workspace",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit stable JSON for supported one-shot commands",
    )
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    parser.add_argument("--debug", action="store_true", help="show Python tracebacks for internal failures")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(
        dest="command",
        title="commands",
        metavar="COMMAND",
        parser_class=_ArgumentParser,
    )
    command_parsers: dict[str, argparse.ArgumentParser] = {}

    def command(
        name: str,
        *,
        help_text: str,
        description: str | None = None,
        epilog: str | None = None,
        common: bool = False,
    ) -> argparse.ArgumentParser:
        visible = show_all_commands or common
        subparser = subparsers.add_parser(
            name,
            help=help_text if visible else argparse.SUPPRESS,
            description=description or help_text,
            epilog=epilog,
            formatter_class=_HelpFormatter,
        )
        subparser.set_defaults(handler=name)
        command_parsers[name] = subparser
        if not visible:
            subparsers._choices_actions = [
                action
                for action in subparsers._choices_actions
                if getattr(action, "dest", None) != name
            ]
        return subparser

    init = command(
        "init",
        help_text="create this project's shell workspace, or one for SCRIPT.py",
        epilog=_example_block(program, "init", "init train.py"),
        common=True,
    )
    init.add_argument(
        "script",
        nargs="?",
        metavar="SCRIPT.py",
        help="Python script for a script workspace",
    )
    init.add_argument("--config", help="initial YAML template for the script workspace")

    execute = command(
        "exec",
        help_text="create and run a tracked command or shell script",
        description=(
            "Create a shell task in the project shell workspace and run it.\n"
            "Place the standard -- separator before an exact program argument vector. A leading\n"
            ".sh, .ps1, .cmd, or .bat file is run with its matching interpreter.\n"
            "Use -c/--command only for a shell command string containing pipelines,\n"
            "redirection, variable expansion, &&, or other shell syntax."
        ),
        epilog=_example_block(
            program,
            "exec -n smoke -- python -V",
            "exec -n setup -- ./scripts/setup.sh",
            r"exec -n setup-ps -- .\scripts\setup.ps1",
            "exec -n train -d -- python train.py --lr 0.001",
            "exec -n train -e CUDA_VISIBLE_DEVICES=0 SEED=42 -- python train.py",
            "exec -n train --env-file .env.train -e SEED=42 -- python train.py",
            "exec --dry-run -n smoke -- python -V",
            'exec -n report -c "python -V > python-version.txt"',
        ),
        common=True,
    )
    execute.add_argument("-n", "--name", help="exact task name; an omitted name is generated safely")
    execute.add_argument("-d", "--detach", action="store_true", help="return after the runner accepts the task")
    execute.add_argument(
        "--dry-run",
        action="store_true",
        help="preview the task and command without creating or running anything",
    )
    execute.add_argument(
        "-c",
        "--command",
        dest="shell_command",
        metavar="COMMAND_STRING",
        help="run one command string through the workspace shell",
    )
    execute.add_argument(
        "-e",
        "--env",
        action="extend",
        nargs="+",
        default=[],
        metavar="KEY=VALUE",
        help="set one or more persisted, visible task environment variables; repeatable",
    )
    execute.add_argument(
        "--env-file",
        action="append",
        default=[],
        metavar="PATH",
        help="load KEY=VALUE lines from a UTF-8 file; repeatable, with -e taking precedence",
    )
    execute.add_argument("command_argv", nargs=argparse.REMAINDER, metavar="COMMAND")

    add = command(
        "add",
        help_text="create named script tasks from YAML without running them",
        epilog=_example_block(
            program,
            "-w train add configs/quick.yaml",
            "--json -w train add sweep.yaml -n ablation",
        ),
        common=True,
    )
    add.add_argument("config", metavar="CONFIG", help="YAML configuration path")
    add.add_argument("-n", "--name", help="task-name prefix; defaults to the YAML filename")

    run = command(
        "run",
        help_text="run or rerun exact tasks; --from creates them first",
        description=(
            "Run exact task names in the selected workspace. By default Pyruns waits for every task\n"
            "and returns non-zero if any task fails. --detach changes only the waiting behavior."
        ),
        epilog=_example_block(
            program,
            "-w train run baseline",
            "-w train run a b c -j 3",
            "-w train run --from configs/quick.yaml -n quick",
            "-w train run --from configs/quick.yaml -n quick --dry-run",
            "-w train run baseline --detach",
        ),
        common=True,
    )
    run.add_argument("tasks", nargs="*", metavar="TASK", help="exact task name")
    run.add_argument("--from", dest="from_config", metavar="CONFIG", help="create tasks from YAML before running")
    run.add_argument("-n", "--name", help="name prefix used with --from")
    run.add_argument("-j", "--workers", type=_positive_int, default=1, help="maximum parallel workers")
    run.add_argument("-m", "--mode", choices=("thread", "process"), default="thread", help="execution backend")
    run.add_argument("-d", "--detach", action="store_true", help="return after the runner accepts all tasks")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="preview task creation from --from without creating or running anything",
    )

    listing = command(
        "ls",
        help_text="list tasks in the selected workspace",
        epilog=_example_block(
            program,
            "ls",
            "ls --status failed --limit 20",
            "--json ls --status running --status queued",
            "ls --trash",
        ),
        common=True,
    )
    listing.add_argument("query", nargs="?", metavar="QUERY", help="case-insensitive text filter")
    listing.add_argument(
        "-s",
        "--status",
        action="append",
        choices=("pending", "queued", "running", "completed", "failed", "cancelled"),
        help="status filter; repeatable",
    )
    listing.add_argument("-n", "--limit", type=_positive_int, help="maximum number of rows")
    listing.add_argument("--sort", choices=("created", "name", "status"), default="created", help="sort key")
    listing.add_argument("--reverse", action="store_true", help="reverse the selected ordering")
    listing.add_argument("--trash", action="store_true", help="list soft-deleted tasks")

    command(
        "status",
        help_text="summarize task states in one workspace",
        epilog=_example_block(program, "status", "--json -w train status"),
    )

    show = command(
        "show",
        help_text="show one task's metadata, command, and paths",
        epilog=_example_block(program, "show smoke", "show smoke@2", "--json -w train show baseline"),
        common=True,
    )
    show.add_argument("task", metavar="TASK[@RUN]", help="exact task name, optionally at one run number")

    logs = command(
        "log",
        help_text="print, follow, or locate one task log",
        epilog=_example_block(
            program,
            "log smoke",
            "log smoke -f",
            "log smoke@2",
            "log smoke --run 2",
            "log smoke --path",
        ),
        common=True,
    )
    logs.add_argument("task", metavar="TASK[@RUN]", help="exact task name, optionally at one run number")
    logs.add_argument("-f", "--follow", action="store_true", help="wait for new output until the task finishes")
    logs.add_argument("--run", type=_positive_int, help="select a historical run number")
    logs.add_argument("--path", action="store_true", help="print only the selected log path")

    wait = command(
        "wait",
        help_text="wait for active tasks and return their result",
    )
    wait.add_argument("tasks", nargs="+", metavar="TASK", help="exact task name")
    wait.add_argument("--timeout", type=_non_negative_float, default=0.0, help="seconds to wait; zero means forever")

    cancel = command(
        "stop",
        help_text="stop active tasks through their owning runner",
        common=True,
    )
    cancel.add_argument("tasks", nargs="+", metavar="TASK", help="exact active task name")
    cancel.add_argument("--timeout", type=_non_negative_float, default=15.0, help="seconds to wait for cancellation")

    remove = command(
        "rm",
        help_text="soft-delete tasks to recoverable workspace trash",
    )
    remove.add_argument("tasks", nargs="+", metavar="TASK", help="exact task name")

    restore = command(
        "restore",
        help_text="restore tasks from workspace trash",
    )
    restore.add_argument("tasks", nargs="+", metavar="TASK", help="exact trashed task name")

    rename = command(
        "mv",
        help_text="rename one inactive task",
    )
    rename.add_argument("task", metavar="TASK", help="exact task name")
    rename.add_argument("new_name", metavar="NEW_NAME", help="new exact task name")

    pin = command(
        "pin",
        help_text="set or clear the pinned state of tasks",
    )
    pin.add_argument("tasks", nargs="+", metavar="TASK", help="exact task name")
    pin.add_argument("--off", action="store_true", help="clear pinned state instead of setting it")

    export = command(
        "export",
        help_text="export task run records as CSV or JSON",
        epilog=_example_block(
            program,
            "export -f json",
            "export baseline -f csv -o report.csv",
            "export -s completed -o -",
        ),
    )
    export.add_argument("tasks", nargs="*", metavar="TASK", help="exact task names; defaults to all")
    export.add_argument(
        "-s",
        "--status",
        action="append",
        choices=("pending", "queued", "running", "completed", "failed", "cancelled"),
        help="status filter; repeatable",
    )
    export.add_argument("-f", "--format", choices=("csv", "json"), default="csv", help="output format")
    export.add_argument("-o", "--output", default="-", metavar="PATH", help="output path; '-' means stdout")

    config = command(
        "config",
        help_text="read or change project settings",
        epilog=_example_block(
            program,
            "config list",
            "config get manager_max_workers",
            "config set manager_max_workers 4",
            "config unset manager_max_workers",
            "config path",
        ),
    )
    config_subparsers = config.add_subparsers(
        dest="config_action",
        title="config actions",
        metavar="ACTION",
        parser_class=_ArgumentParser,
    )
    config_list = config_subparsers.add_parser("list", help="list effective settings")
    config_list.set_defaults(config_action="list")
    config_get = config_subparsers.add_parser("get", help="print one effective setting")
    config_get.add_argument("key", metavar="KEY")
    config_set = config_subparsers.add_parser("set", help="set one project setting")
    config_set.add_argument("key", metavar="KEY")
    config_set.add_argument("value", metavar="VALUE", help="YAML scalar, list, or mapping")
    config_unset = config_subparsers.add_parser("unset", help="restore one setting to its default")
    config_unset.add_argument("key", metavar="KEY")
    config_subparsers.add_parser("path", help="print the project settings path")

    command(
        "metrics",
        help_text="print a CPU, memory, and GPU snapshot",
        epilog=_example_block(program, "metrics", "--json metrics"),
    )

    ui = command(
        "ui",
        help_text="start the optional Web UI",
        description=(
            "Start the Web UI workspace launcher, open an exact existing WORKSPACE, or\n"
            "initialize and open SCRIPT.py. Use 'ui shell' for the project shell workspace.\n"
            "Global -w/--workspace and --json do not apply to UI commands."
        ),
        epilog=_example_block(
            program,
            "ui",
            "ui train.py",
            "ui train.py --config settings.yaml",
            "ui shell --no-browser",
        ),
    )
    ui.add_argument(
        "target",
        nargs="?",
        metavar="WORKSPACE|SCRIPT.py",
        help="existing workspace name/path, 'shell', or Python script path",
    )
    ui.add_argument("--config", help="YAML template imported for the script workspace")
    _add_browser_options(ui)

    dev = command(
        "dev",
        help_text="start the Web UI with hot reload",
        description=(
            "Initialize and open SCRIPT.py with Web UI hot reload.\n"
            "Global -w/--workspace and --json do not apply to UI commands."
        ),
    )
    dev.add_argument("script", metavar="SCRIPT.py", help="Python script workspace to open")
    dev.add_argument("--config", help="YAML template imported for the script workspace")
    _add_browser_options(dev)

    help_parser = command(
        "help",
        help_text="show top-level or command-specific help",
    )
    help_parser.add_argument(
        "topic",
        nargs="?",
        metavar="COMMAND",
        choices=tuple(command_parsers),
        help="command name",
    )
    help_parser.add_argument("-a", "--all", action="store_true", dest="all_commands", help="list every command")

    return parser, command_parsers


def main(argv: Sequence[str] | None = None, *, prog: str | None = None) -> int:
    """Parse and execute one Pyruns command."""

    program = prog or (_invoked_program() if argv is None else "pyr")
    parser, command_parsers = build_parser(program)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.no_color:
        os.environ["NO_COLOR"] = "1"

    try:
        directory = os.path.abspath(os.path.expanduser(os.path.expandvars(args.directory)))
        if not os.path.isdir(directory):
            parser.error(f"directory does not exist: {args.directory}")
        os.chdir(directory)

        if args.command is None:
            parser.print_help()
            return 0

        if args.command == "help":
            if args.all_commands and args.topic:
                command_parsers["help"].error("--all cannot be combined with COMMAND")
            if args.topic:
                command_parsers[args.topic].print_help()
            elif args.all_commands:
                full_parser, _ = build_parser(program, show_all_commands=True)
                full_parser.print_help()
            else:
                parser.print_help()
            return 0

        context = CliContext(
            directory=directory,
            workspace=args.workspace,
            json_output=bool(args.json_output),
            debug=bool(args.debug),
            program=parser.prog,
        )

        from pyruns.cli.commands import CliError, dispatch

        return int(dispatch(context, args) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        from pyruns.cli.commands import CliError

        if isinstance(exc, CliError):
            print(f"{parser.prog}: error: {exc}", file=sys.stderr)
            return exc.exit_code
        if bool(getattr(args, "debug", False)):
            raise
        print(f"{parser.prog}: internal error: {exc}", file=sys.stderr)
        print("Run again with --debug to show the traceback.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
