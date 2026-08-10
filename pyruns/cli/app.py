"""Declarative, script-friendly command line interface for Pyruns."""

from __future__ import annotations

import argparse
import difflib
import math
import os
import shutil
import sys
import textwrap
from dataclasses import dataclass
from typing import Sequence

from pyruns import __version__


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keep examples readable while retaining argparse's option formatting."""

    def __init__(self, prog: str) -> None:
        columns = shutil.get_terminal_size(fallback=(100, 24)).columns
        super().__init__(prog, max_help_position=30, width=max(40, min(columns, 100)))

    def _fill_text(self, text: str, width: int, indent: str) -> str:
        """Reflow prose while preserving the indentation of examples and notes."""

        rendered: list[str] = []
        paragraph: list[str] = []

        def flush_paragraph() -> None:
            if not paragraph:
                return
            rendered.extend(
                textwrap.wrap(
                    " ".join(paragraph),
                    width=width,
                    initial_indent=indent,
                    subsequent_indent=indent,
                    break_long_words=False,
                    break_on_hyphens=False,
                )
            )
            paragraph.clear()

        for line in text.splitlines():
            if not line.strip():
                flush_paragraph()
                rendered.append("")
            elif line[:1].isspace():
                flush_paragraph()
                leading = line[: len(line) - len(line.lstrip())]
                prefix = indent + leading
                rendered.extend(
                    textwrap.wrap(
                        line.lstrip(),
                        width=width,
                        initial_indent=prefix,
                        subsequent_indent=prefix + "  ",
                        replace_whitespace=False,
                        break_long_words=False,
                        break_on_hyphens=False,
                    )
                )
            else:
                paragraph.append(line.strip())
        flush_paragraph()
        return "\n".join(rendered)


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser with concise, consistent errors."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        if message == "unrecognized arguments: --json":
            message = (
                "--json is command-specific; append it only to a command "
                "that advertises --json in its help"
            )
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")

    def _check_value(self, action: argparse.Action, value: object) -> None:
        """Keep command typos concise while preserving exact matching."""

        choices = action.choices
        command_like = isinstance(action, argparse._SubParsersAction) or action.dest == "topic"
        if command_like and choices is not None and value not in choices:
            choice_names = [str(choice) for choice in choices]
            suggestions = difflib.get_close_matches(
                str(value),
                choice_names,
                n=1,
                cutoff=0.75,
            )
            kind = "action" if action.dest.endswith("_action") else "command"
            message = f"unknown {kind} '{value}'"
            if suggestions:
                message += f"\nDid you mean '{suggestions[0]}'?"
            self.error(message)
        super()._check_value(action, value)


@dataclass(frozen=True)
class CliContext:
    """Resolved invocation context shared with command handlers."""

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


def _configure_output_streams() -> None:
    """Keep Unicode output usable on restrictive terminal encodings."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (OSError, ValueError):
            pass


def _example_block(
    program: str,
    *examples: str,
    notes: Sequence[str] = (),
) -> str:
    sections = ["Examples:\n" + "\n".join(f"  {program} {example}" for example in examples)]
    if notes:
        sections.append("Notes:\n" + "\n".join(f"  {note}" for note in notes))
    return "\n\n".join(sections)


def _exec_help_epilog(program: str) -> str:
    """Return the decision-oriented help shown by ``pyr help exec``."""

    return (
        "Examples:\n"
        "  Choose a command form:\n\n"
        "  Exact argv (recommended for Python and ordinary programs):\n"
        f"    {program} exec -n smoke -- python -V\n"
        f"    {program} exec -n train -- python train.py --epochs 10\n"
        f"    {program} exec -n tests -- python -m pytest -q\n"
        "    -- ends Pyruns option parsing. Each following token is one program argument.\n"
        "    Quote only an individual argument containing spaces; shell syntax is not expanded.\n\n"
        "  Existing shell script (tracked replacement for running the script directly):\n"
        f"    {program} exec -n setup -- ./scripts/setup.sh arg1\n"
        f"    {program} exec -n setup-ps -- .\\scripts\\setup.ps1 arg1\n"
        f"    {program} exec -n setup-cmd -- .\\scripts\\setup.cmd arg1\n"
        "    .sh, .ps1, .cmd, and .bat select their matching interpreter automatically.\n"
        "    Pyruns records stdout/stderr, duration, exit code, working directory, and source state.\n\n"
        "  Shell expression (only for pipes, redirects, expansion, globs, or chaining):\n"
        f"    {program} exec -n report -c \"python eval.py > metrics.txt\"\n"
        f"    {program} exec -n pipeline -c \"python prep.py && python train.py\"\n"
        "    -c accepts exactly one quoted string and runs it through the stored workspace shell.\n\n"
        "Environment:\n"
        f"  {program} exec -n gpu0 -e CUDA_VISIBLE_DEVICES=0 SEED=42 -- python train.py\n"
        f"  {program} exec -n gpu0 --env-file .env.train -e SEED=42 -- python train.py\n"
        "  One -e accepts multiple KEY=VALUE entries; repeating -e is optional.\n"
        "  -e and --env-file are saved with the task for CLI and Web UI reruns.\n"
        "  Variables inherited from the invoking terminal affect this run but are not saved.\n"
        "  PYTHONUNBUFFERED=1, PYTHONIOENCODING=utf-8, and PYTHONUTF8=1 are automatic.\n"
        "  Persisted environment values are visible in task_info.json and 'show'; do not store secrets.\n\n"
        "Execution and follow-up:\n"
        f"  {program} exec --dry-run -n smoke -- python -V       preview without creating anything\n"
        f"  {program} exec -n train -d -- python train.py       submit and return after acceptance\n"
        f"  {program} -w shell show train                       inspect command, env, and paths\n"
        f"  {program} -w shell log train -f                     follow the active log\n"
        f"  {program} -w shell wait train                        wait for the final result\n"
        f"  {program} -w shell run train                         rerun the saved task\n"
        "  Foreground exec follows the log and returns the task result; -d changes waiting only.\n"
        "  Ctrl+C during foreground exec requests cancellation of the task submitted by this call.\n"
        "  --dry-run and -d/--detach are mutually exclusive."
    )


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid port: {value}") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _non_empty(value: str) -> str:
    text = str(value)
    if not text.strip():
        raise argparse.ArgumentTypeError("value must not be empty")
    return text


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive integer: {value}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _run_index(value: str) -> int:
    from pyruns.utils.info_io import MAX_RUN_HISTORY_SLOTS

    text = str(value)
    if not text.isdecimal():
        raise argparse.ArgumentTypeError("run must be a positive integer")
    normalized = text.lstrip("0") or "0"
    maximum = str(MAX_RUN_HISTORY_SLOTS)
    if (
        normalized == "0"
        or len(normalized) > len(maximum)
        or (len(normalized) == len(maximum) and normalized > maximum)
    ):
        raise argparse.ArgumentTypeError(
            f"run must be between 1 and {MAX_RUN_HISTORY_SLOTS}"
        )
    return int(normalized)


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


def _add_json_option(
    parser: argparse.ArgumentParser,
    *,
    help_text: str = "emit strict versioned JSON for this command",
) -> None:
    """Add machine output only to the command scope that owns it."""

    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=argparse.SUPPRESS,
        help=help_text,
    )


def _validate_leading_option_scope(
    argv: Sequence[str],
    parser: argparse.ArgumentParser,
    command_names: set[str],
) -> None:
    """Reject command options placed before COMMAND with an actionable error."""

    index = 0
    while index < len(argv):
        token = str(argv[index])
        if token in command_names:
            return
        if token in {"-h", "--help", "--debug", "--version"}:
            index += 1
            continue
        if token in {"-C", "--directory", "-w", "--workspace"}:
            index += 2
            continue
        if token.startswith(("--directory=", "--workspace=")):
            index += 1
            continue
        if (token.startswith("-C") or token.startswith("-w")) and len(token) > 2:
            index += 1
            continue
        if token == "--json":
            parser.error(
                "--json is command-specific; place it after a command that advertises --json"
            )
        if token in {"-p", "--port"} or token.startswith(("-p", "--port=")):
            parser.error(
                f"-p/--port belongs to ui and dev; use '{parser.prog} ui --port PORT'"
            )
        if token in {"--browser", "--no-browser"}:
            parser.error(
                f"{token} belongs to ui and dev; place it after the command"
            )
        if token == "--":
            parser.error("COMMAND must appear before '--'")
        if token.startswith("-"):
            parser.error(
                f"unrecognized global option: {token}\n"
                "Put command-specific options after COMMAND"
            )
        return


def build_parser(
    program: str = "pyr",
    *,
    show_all_commands: bool = False,
) -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]:
    """Build the complete CLI parser and its command-help index."""

    alternate = "pyruns" if program == "pyr" else "pyr"
    command_overview = (
        "All command groups:\n"
        "  task setup       init  exec  add  run\n"
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
            "Pyruns saves terminal commands and Python experiments as named tasks that you can\n"
            "inspect and rerun. Logs, duration, exit codes, and source state live under _pyruns_.\n"
            f"{program} and {alternate} are identical; every invocation performs one operation and exits.\n"
            "The Web UI starts only with 'ui' (normal) or 'dev' (development); a bare command\n"
            "prints this help.\n\n"
            "Model: project -> workspace -> task -> numbered run\n"
            "  project    nearest project found from the current directory (or -C PATH)\n"
            "             and its parents; owns one _pyruns_ directory\n"
            "  workspace  'shell' for terminal commands, or one workspace per Python script\n"
            "  task       a saved command or YAML configuration with an exact name\n"
            "  run        one execution; rerunning a task keeps numbered history\n\n"
            "Quick start -- track one terminal command:\n"
            f"  {program} exec -n check -- python -V   create and run task 'check'\n"
            f"  {program} ls                            list tasks\n"
            f"  {program} show check                    inspect command and run history\n"
            f"  {program} log check                     print its output\n"
            f"  {program} run check                     rerun the saved task\n"
            f"  {program} ui shell                      open the shell workspace in the Web UI"
        ),
        epilog=(
            command_overview
            + "Notes:\n"
            "  Put context options (-C, -w, --debug) before COMMAND.\n"
            f"  For automation, append --json where advertised; output includes schema_version 1.\n"
            "  Task names are exact. With several workspaces, select one with -w.\n"
            "  exec creates the shell workspace automatically.\n\n"
            "More help:\n"
            f"  {program} help COMMAND\n"
            f"  {program} help -a        list all commands\n"
            f"  {program} COMMAND --help    show command options (for example, ui --port)\n\n"
            "Exit status: 0 success, 1 operation failed, 2 invalid usage, 130 interrupted."
        ),
    )
    parser.set_defaults(json_output=False)
    parser.add_argument(
        "-C",
        "--directory",
        type=_non_empty,
        default=".",
        metavar="PATH",
        help="resolve the project as if Pyruns was started in PATH; place before COMMAND",
    )
    parser.add_argument(
        "-w",
        "--workspace",
        type=_non_empty,
        metavar="NAME|PATH|SCRIPT.py",
        help="select an exact task workspace; use 'shell'; place before COMMAND",
    )
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
        json_help: str | None = "emit strict versioned JSON for this command",
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
        if json_help is not None:
            _add_json_option(subparser, help_text=json_help)
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
        description=(
            "Create workspace metadata and exit without starting a task or the Web UI.\n"
            "With no SCRIPT.py, initialize the project's shared shell workspace. With a Python\n"
            "script, initialize its script workspace and optionally import an initial YAML template."
        ),
        epilog=_example_block(
            program,
            "init",
            "init train.py",
            "init train.py --config configs/default.yaml",
            notes=(
                "exec creates the shell workspace automatically, so 'init' is optional for exec.",
                "--config requires SCRIPT.py and is copied as the script workspace template.",
                "Use 'ui ...' explicitly when a browser interface is wanted.",
            ),
        ),
        common=True,
    )
    init.add_argument(
        "script",
        nargs="?",
        type=_non_empty,
        metavar="SCRIPT.py",
        help="Python script for a script workspace",
    )
    init.add_argument(
        "--config",
        type=_non_empty,
        help="initial YAML template for the script workspace",
    )

    execute = command(
        "exec",
        help_text="create and run a tracked command or shell script",
        description=(
            "Create a shell task in the project shell workspace and run it.\n"
            "The default form is '-- PROGRAM ARG ...': -- ends Pyruns option parsing and stores\n"
            "every following item as an exact argv element. It does not enable shell parsing.\n"
            "Use -c/--command only when the command intentionally needs pipes, redirects, variable\n"
            "expansion, globs, &&, or other shell syntax. A leading .sh, .ps1, .cmd, or .bat file\n"
            "is launched with its matching interpreter while Pyruns records its log and duration."
        ),
        epilog=_exec_help_epilog(program),
        common=True,
    )
    execute.add_argument(
        "-n",
        "--name",
        type=_non_empty,
        help="exact task name; an omitted name is generated safely",
    )
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
        type=_non_empty,
        metavar="COMMAND_STRING",
        help="run exactly one quoted command string through the workspace shell",
    )
    execute.add_argument(
        "-e",
        "--env",
        action="extend",
        nargs="+",
        default=[],
        metavar="KEY=VALUE",
        help="persist one or more KEY=VALUE entries after one -e; repeatable",
    )
    execute.add_argument(
        "--env-file",
        action="append",
        type=_non_empty,
        default=[],
        metavar="PATH",
        help="persist KEY=VALUE lines from UTF-8 files; later files then -e take precedence",
    )
    execute.add_argument(
        "command_argv",
        nargs=argparse.REMAINDER,
        metavar="PROGRAM [ARG ...]",
        help="exact program and arguments after --; shell syntax is not interpreted",
    )

    add = command(
        "add",
        help_text="create named script tasks from YAML without running them",
        description=(
            "Read and validate YAML in a selected Python script workspace, expand any supported\n"
            "batch values, and create immutable task snapshots without running them. This command\n"
            "does not apply to the shell workspace."
        ),
        epilog=_example_block(
            program,
            "-w train add configs/quick.yaml",
            "-w train add sweep.yaml -n ablation --json",
            notes=(
                "Select a script workspace with -w when discovery is ambiguous.",
                "-n is a task-name prefix; expanded batches receive deterministic suffixes.",
                "Success prints the exact created names; use 'run TASK ...' to execute them later.",
            ),
        ),
        common=True,
    )
    add.add_argument("config", type=_non_empty, metavar="CONFIG", help="YAML configuration path")
    add.add_argument(
        "-n",
        "--name",
        type=_non_empty,
        help="task-name prefix; defaults to the YAML filename",
    )

    run = command(
        "run",
        help_text="run exact tasks, or create and run tasks from YAML",
        description=(
            "Run or rerun exact task names in the selected shell or script workspace. Alternatively,\n"
            "--config CONFIG creates script tasks from YAML and immediately runs them. By default Pyruns\n"
            "waits for every requested task and returns non-zero if any task fails."
        ),
        epilog=_example_block(
            program,
            "-w train run baseline",
            "-w train run a b c -j 3",
            "-w train run --config configs/quick.yaml -n quick",
            "-w train run --config configs/quick.yaml -n quick --dry-run",
            "-w train run baseline --detach",
            notes=(
                "Use either TASK names or --config CONFIG, never both.",
                "--name and --dry-run are valid only with --config.",
                "--dry-run validates and expands YAML but creates and runs nothing.",
                "--dry-run and -d/--detach are mutually exclusive.",
                "-j/--jobs controls how many tasks may run concurrently.",
                "By default Pyruns waits for every task and reports aggregate failure.",
                "Ctrl+C while waiting requests cancellation of tasks submitted by this run command.",
                "--detach changes waiting only; accepted tasks continue under the hidden runner.",
                "A partial runner acceptance is reported with claimed and unclaimed names and exits 1.",
            ),
        ),
        common=True,
    )
    run.add_argument("tasks", nargs="*", type=_non_empty, metavar="TASK", help="exact task name")
    run.add_argument(
        "--config",
        type=_non_empty,
        metavar="CONFIG",
        help="create tasks from this YAML configuration before running",
    )
    run.add_argument("-n", "--name", type=_non_empty, help="task-name prefix used with --config")
    run.add_argument(
        "-j",
        "--jobs",
        type=_positive_int,
        default=1,
        metavar="N",
        help="maximum number of tasks to run concurrently",
    )
    run.add_argument("-d", "--detach", action="store_true", help="return after the runner accepts all tasks")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="preview task creation from --config without creating or running anything",
    )

    listing = command(
        "ls",
        help_text="list tasks in the selected workspace",
        description=(
            "List task summaries from one workspace. QUERY performs a case-insensitive text search;\n"
            "repeat --status to include several states. Use --trash to inspect recoverable deleted\n"
            "tasks instead of the active task list. Pinned active tasks always remain first."
        ),
        epilog=_example_block(
            program,
            "ls",
            "-w train ls loss",
            "ls --status failed --limit 20",
            "ls --status running --status queued --json",
            "ls --sort name --reverse",
            "ls --trash",
            notes=(
                "Without -w, Pyruns uses the only workspace discovered from the current directory.",
                "--status is repeatable; --limit applies after filtering and ordering.",
                "--reverse reverses each pinned/unpinned group; pinned tasks still remain first.",
                "Use --json for strict, versioned machine-readable task summaries.",
            ),
        ),
        common=True,
    )
    listing.add_argument("query", nargs="?", metavar="QUERY", help="case-insensitive text filter")
    listing.add_argument(
        "-s",
        "--status",
        action="append",
        choices=("pending", "queued", "running", "completed", "failed", "cancelled"),
        metavar="STATUS",
        help="filter by pending, queued, running, completed, failed, or cancelled; repeatable",
    )
    listing.add_argument("-n", "--limit", type=_positive_int, help="maximum number of rows")
    listing.add_argument("--sort", choices=("created", "name", "status"), default="created", help="sort key")
    listing.add_argument(
        "--reverse",
        action="store_true",
        help="reverse ordering within pinned and unpinned groups",
    )
    listing.add_argument("--trash", action="store_true", help="list soft-deleted tasks")

    command(
        "status",
        help_text="summarize task states in one workspace",
        description=(
            "Print the selected workspace path and kind, total task count, counts for every lifecycle\n"
            "state, and exact names of queued or running tasks. This is a one-shot snapshot."
        ),
        epilog=_example_block(
            program,
            "status",
            "-w shell status",
            "-w train status --json",
            notes=(
                "Use 'metrics' for host CPU, memory, and GPU data; status reports task state only.",
                "-w remains a context option before 'status'; --json may follow it.",
            ),
        ),
        common=True,
    )

    show = command(
        "show",
        help_text="show one task's metadata, command, and paths",
        description=(
            "Show one exact task's status, pin, payload, command, working directory, environment, runtime\n"
            "history, and log paths. Use --run RUN or append @RUN to select one historical run."
        ),
        epilog=_example_block(
            program,
            "show smoke",
            "show smoke@2",
            "show smoke --run 2",
            "-w train show baseline --json",
            notes=(
                "TASK is an exact name. New task names reserve @ for the TASK@RUN shorthand.",
                "TASK@RUN and --run RUN select the same historical run.",
                "TASK@RUN cannot be combined with --run.",
                "Use 'log TASK[@RUN]' to print the corresponding log content.",
            ),
        ),
        common=True,
    )
    show.add_argument("task", metavar="TASK[@RUN]", help="exact task name, optionally at one run number")
    show.add_argument("--run", type=_run_index, help="select a historical run number")

    logs = command(
        "log",
        help_text="print, follow, or locate one task log",
        description=(
            "Print the latest log, select a historical run, follow an active run as raw stdout, or\n"
            "print only the resolved log path. TASK@RUN and --run RUN select the same history."
        ),
        epilog=_example_block(
            program,
            "log smoke",
            "log smoke -f",
            "log smoke@2",
            "log smoke --run 2",
            "log smoke --path",
            "log smoke@2 --path --json",
            notes=(
                "TASK@RUN cannot be combined with --run or --follow.",
                "--follow cannot be combined with --run or --path and rejects a pending task.",
                "Raw log output is intentionally not JSON; combine --json with --path instead.",
                "--follow streams bytes until the task finishes; it is not an interactive terminal.",
                "Ctrl+C stops following and returns 130; it does not stop the task.",
            ),
        ),
        common=True,
        json_help="with --path, emit a JSON object containing the selected log path",
    )
    logs.add_argument("task", metavar="TASK[@RUN]", help="exact task name, optionally at one run number")
    logs.add_argument("-f", "--follow", action="store_true", help="wait for new output until the task finishes")
    logs.add_argument("--run", type=_run_index, help="select a historical run number")
    logs.add_argument("--path", action="store_true", help="print only the selected log path")

    wait = command(
        "wait",
        help_text="wait for active tasks and return their result",
        description=(
            "Wait until every exact queued or running task reaches a final state, then return success\n"
            "only when all of them completed successfully. Pending tasks have not been submitted and\n"
            "are rejected instead of waiting forever."
        ),
        epilog=_example_block(
            program,
            "wait train",
            "-w train wait seed1 seed2 --timeout 600",
            notes=(
                "--timeout 0 means wait indefinitely; a positive timeout is measured in seconds.",
                "Timeout or Ctrl+C stops waiting only; the tasks continue running.",
                "Exit status is 1 for task failure, cancellation, or timeout, and 130 if interrupted.",
            ),
        ),
        common=True,
    )
    wait.add_argument("tasks", nargs="+", metavar="TASK", help="exact task name")
    wait.add_argument("--timeout", type=_non_negative_float, default=0.0, help="seconds to wait; zero means forever")

    cancel = command(
        "stop",
        help_text="stop active tasks through their owning runner",
        description=(
            "Persist a cancellation request for each exact queued or running task and wait for its\n"
            "owning runner to finish cancellation. If that runner's lease has expired, queued work\n"
            "becomes 'cancelled' and stale running work becomes 'failed' without killing its PID."
        ),
        epilog=_example_block(
            program,
            "stop train",
            "-w train stop seed1 seed2 --timeout 30",
            notes=(
                "Only active queued or running tasks can be stopped.",
                "The default cancellation wait is 15 seconds; this command does not delete the task.",
                "Exit status is 0 only when every target ends as cancelled; stale running work returns 1.",
            ),
        ),
        common=True,
    )
    cancel.add_argument("tasks", nargs="+", metavar="TASK", help="exact active task name")
    cancel.add_argument(
        "--timeout",
        type=_non_negative_float,
        default=15.0,
        help="seconds to wait for cancellation; zero means forever",
    )

    remove = command(
        "rm",
        help_text="soft-delete tasks to recoverable workspace trash",
        description=(
            "Move exact inactive tasks into this workspace's trash. Removal is immediate and\n"
            "non-interactive, but it is recoverable with 'restore'; task files are not permanently\n"
            "deleted by this command."
        ),
        epilog=_example_block(
            program,
            "rm obsolete-run",
            "-w train rm seed1 seed2",
            "ls --trash",
            notes=(
                "rm has no confirmation prompt and no --yes option.",
                "Queued or running tasks must be stopped before removal.",
            ),
        ),
    )
    remove.add_argument("tasks", nargs="+", metavar="TASK", help="exact task name")

    restore = command(
        "restore",
        help_text="restore tasks from workspace trash",
        description=(
            "Move exact trashed tasks back into the active task list with their metadata, payloads,\n"
            "logs, and run history intact."
        ),
        epilog=_example_block(
            program,
            "ls --trash",
            "restore obsolete-run",
            "-w train restore obsolete-run",
            notes=(
                "Use the exact task name or exact trash entry name shown by 'ls --trash'.",
                "Restore fails rather than overwriting an active task with the same name.",
            ),
        ),
    )
    restore.add_argument("tasks", nargs="+", metavar="TASK", help="exact trashed task name")

    rename = command(
        "mv",
        help_text="rename one inactive task",
        description=(
            "Rename one exact inactive task and its task directory while preserving its payload,\n"
            "metadata, logs, and run history."
        ),
        epilog=_example_block(
            program,
            "mv baseline baseline-lr1e3",
            "-w train mv seed1 seed1-final",
            notes=(
                "Queued or running tasks cannot be renamed.",
                "NEW_NAME must be a valid unused exact task name; @ is reserved for run references.",
            ),
        ),
    )
    rename.add_argument("task", metavar="TASK", help="exact task name")
    rename.add_argument("new_name", metavar="NEW_NAME", help="new exact task name")

    pin = command(
        "pin",
        help_text="set or clear the pinned state of tasks",
        description=(
            "Mark exact tasks as pinned for prominent, stable ordering in task views, or clear that\n"
            "state with --off. Pinning does not start, stop, or otherwise modify a task run."
        ),
        epilog=_example_block(
            program,
            "pin baseline",
            "pin seed1 seed2",
            "pin baseline --off",
            notes=(
                "The operation is idempotent and accepts both active and inactive tasks.",
            ),
        ),
    )
    pin.add_argument("tasks", nargs="+", metavar="TASK", help="exact task name")
    pin.add_argument("--off", action="store_true", help="clear pinned state instead of setting it")

    export = command(
        "export",
        help_text="export task run records as CSV or JSON",
        description=(
            "Export lifecycle and monitoring records for selected tasks, or every task when no names\n"
            "are given. Each historical task run becomes one output row."
        ),
        epilog=_example_block(
            program,
            "export",
            "export -f json",
            "export baseline -f csv -o report.csv",
            "export -s completed -o -",
            notes=(
                "The default format is CSV and the default output '-' means stdout.",
                "--status is repeatable. Runs without monitor metrics still include lifecycle fields.",
                "Choose CSV or JSON records with --format; output filenames do not select a format.",
            ),
        ),
        json_help=None,
    )
    export.add_argument("tasks", nargs="*", metavar="TASK", help="exact task names; defaults to all")
    export.add_argument(
        "-s",
        "--status",
        action="append",
        choices=("pending", "queued", "running", "completed", "failed", "cancelled"),
        metavar="STATUS",
        help="filter by pending, queued, running, completed, failed, or cancelled; repeatable",
    )
    export.add_argument("-f", "--format", choices=("csv", "json"), help="output format; defaults to CSV")
    export.add_argument("-o", "--output", default="-", metavar="PATH", help="output path; '-' means stdout")

    config = command(
        "config",
        help_text="read or change project settings",
        description=(
            "Read or update settings shared by every workspace in the current project. Config does\n"
            "not require -w. Values passed to 'set' use YAML syntax and are validated against the\n"
            "known setting's type and allowed range before anything is persisted."
        ),
        epilog=_example_block(
            program,
            "config list",
            "config get monitor_scrollback",
            "config set monitor_scrollback 200000",
            "config set global_env '{CUDA_VISIBLE_DEVICES: 0, SEED: 42}'",
            "config unset monitor_scrollback",
            "config path",
            notes=(
                "Settings are project-wide; -w is unnecessary and does not change their scope.",
                "VALUE is one YAML scalar, list, or mapping; quote mappings as one shell argument.",
                "global_env is persisted for Web UI runs; CLI tasks inherit the invoking terminal.",
                "Use 'exec -e' or '--env-file' for environment values saved with a task.",
                "Use 'unset' to restore the built-in default rather than writing a guessed value.",
            ),
        ),
        json_help=None,
    )
    config_subparsers = config.add_subparsers(
        dest="config_action",
        title="config actions",
        metavar="ACTION",
        parser_class=_ArgumentParser,
    )
    config_list = config_subparsers.add_parser(
        "list",
        help="list effective settings",
        description="Print every effective project setting after defaults and saved values are merged.",
        epilog=_example_block(program, "config list", "config list --json"),
        formatter_class=_HelpFormatter,
    )
    config_list.set_defaults(config_action="list")
    _add_json_option(config_list)
    config_get = config_subparsers.add_parser(
        "get",
        help="print one effective setting",
        description="Print one known setting after defaults and saved values are merged.",
        epilog=_example_block(program, "config get monitor_scrollback"),
        formatter_class=_HelpFormatter,
    )
    config_get.add_argument("key", metavar="KEY", help="known project setting name")
    _add_json_option(config_get)
    config_set = config_subparsers.add_parser(
        "set",
        help="set one project setting",
        description=(
            "Parse VALUE as YAML, validate it for KEY, persist it in the project settings file,\n"
            "and print the normalized saved value."
        ),
        epilog=_example_block(
            program,
            "config set monitor_scrollback 200000",
            "config set gpu_scheduler_device_ids '[0, 1]'",
            "config set global_env '{CUDA_VISIBLE_DEVICES: 0, SEED: 42}'",
            notes=(
                "Quote lists and mappings so the shell passes VALUE as one argument.",
                "Environment variable names and values are validated before global_env is saved.",
            ),
        ),
        formatter_class=_HelpFormatter,
    )
    config_set.add_argument("key", metavar="KEY", help="known project setting name")
    config_set.add_argument("value", metavar="VALUE", help="YAML scalar, list, or mapping")
    _add_json_option(config_set)
    config_unset = config_subparsers.add_parser(
        "unset",
        help="restore one setting to its default",
        description="Remove one saved override so its built-in default becomes effective.",
        epilog=_example_block(program, "config unset monitor_scrollback"),
        formatter_class=_HelpFormatter,
    )
    config_unset.add_argument("key", metavar="KEY", help="known project setting name")
    _add_json_option(config_unset)
    config_path = config_subparsers.add_parser(
        "path",
        help="print the project settings path",
        description="Print the normalized path of this project's settings YAML file.",
        epilog=_example_block(program, "config path"),
        formatter_class=_HelpFormatter,
    )
    _add_json_option(config_path)

    command(
        "metrics",
        help_text="print a CPU, memory, and GPU snapshot",
        description=(
            "Sample host CPU, memory, and available GPU metrics once, print the result, and exit.\n"
            "This command does not require a workspace and does not start a live monitor."
        ),
        epilog=_example_block(
            program,
            "metrics",
            "metrics --json",
            notes=(
                "Use Web UI Monitor for a continuously updated view.",
                "Use --json for a strict, versioned machine-readable snapshot.",
            ),
        ),
    )

    ui = command(
        "ui",
        help_text="start the optional Web UI",
        description=(
            "Start the Web UI workspace launcher, open an exact existing WORKSPACE, or\n"
            "initialize and open SCRIPT.py. Use 'ui shell' for the project shell workspace.\n"
            "UI commands do not accept -w/--workspace or --json."
        ),
        epilog=_example_block(
            program,
            "ui",
            "ui train.py",
            "ui train.py --config settings.yaml",
            "ui shell",
            "ui shell --no-browser",
            "ui shell --no-browser --port 8099",
            notes=(
                "With no target, ui opens the workspace launcher and does not guess a workspace.",
                "Pass shell, an existing workspace name/path, or a Python script directly after ui.",
                "Do not write '-w shell ui'; pass the workspace directly after 'ui'.",
                "Use 'ui --help' to discover --port, --browser, and --no-browser.",
                "Startup prints a private tokenized loopback URL; do not share it.",
                "--no-browser keeps the server headless; stop it with Ctrl+C or the service manager.",
            ),
        ),
        common=True,
        json_help=None,
    )
    ui.add_argument(
        "target",
        nargs="?",
        type=_non_empty,
        metavar="WORKSPACE|SCRIPT.py",
        help="existing workspace name/path, 'shell', or Python script path",
    )
    ui.add_argument(
        "--config",
        type=_non_empty,
        help="YAML template imported for the script workspace",
    )
    _add_browser_options(ui)

    dev = command(
        "dev",
        help_text="start the Web UI with hot reload",
        description=(
            "Initialize and open SCRIPT.py with Web UI hot reload.\n"
            "This is a development server, not the normal production UI entry. Global\n"
            "UI commands do not accept -w/--workspace or --json."
        ),
        json_help=None,
        epilog=_example_block(
            program,
            "dev train.py",
            "dev train.py --config configs/default.yaml",
            "dev train.py --no-browser --port 8099",
            notes=(
                "Use 'ui' for normal use; dev enables server hot reload for Pyruns development.",
                "SCRIPT.py is required and --config imports the initial YAML template.",
            ),
        ),
    )
    dev.add_argument(
        "script",
        type=_non_empty,
        metavar="SCRIPT.py",
        help="Python script workspace to open",
    )
    dev.add_argument(
        "--config",
        type=_non_empty,
        help="YAML template imported for the script workspace",
    )
    _add_browser_options(dev)

    help_parser = command(
        "help",
        help_text="show top-level or command-specific help",
        description=(
            "Show the concise top-level guide, the complete command index, or detailed help for one\n"
            "command. Help is read-only and never creates a workspace or starts the Web UI."
        ),
        json_help=None,
        epilog=_example_block(
            program,
            "help",
            "help -a",
            "help exec",
            "exec --help",
            notes=(
                "'help COMMAND' and 'COMMAND --help' print the same command-specific guide.",
                "Use 'help -a' when the concise top-level command list hides an advanced command.",
            ),
        ),
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


def _main(argv: Sequence[str] | None = None, *, prog: str | None = None) -> int:
    """Parse and execute one Pyruns command."""

    _configure_output_streams()
    program = prog or (_invoked_program() if argv is None else "pyr")
    parser, command_parsers = build_parser(program)
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    _validate_leading_option_scope(raw_argv, parser, set(command_parsers))
    args = parser.parse_args(raw_argv)

    try:
        directory = os.path.abspath(os.path.expanduser(os.path.expandvars(args.directory)))
        if not os.path.isdir(directory):
            parser.error(f"directory does not exist: {args.directory}")
        os.chdir(directory)

        if args.command is None:
            if args.workspace:
                parser.error("-w/--workspace requires a command")
            parser.print_help()
            return 0

        if args.command == "help":
            if args.workspace:
                command_parsers["help"].error("help does not use -w/--workspace")
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
    except BrokenPipeError:
        raise
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        from pyruns.cli.commands import CliError, CliUsageError

        if isinstance(exc, CliError):
            if isinstance(exc, CliUsageError):
                handler = str(getattr(args, "handler", "") or "")
                command_parsers.get(handler, parser).print_usage(sys.stderr)
            print(f"{parser.prog}: error: {exc}", file=sys.stderr)
            return exc.exit_code
        if bool(getattr(args, "debug", False)):
            raise
        print(f"{parser.prog}: internal error: {exc}", file=sys.stderr)
        print("Run again with --debug to show the traceback.", file=sys.stderr)
        return 1


def _silence_broken_pipe() -> None:
    """Prevent a second BrokenPipeError while Python flushes stdout at exit."""

    try:
        stdout_fd = sys.stdout.fileno()
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
    except (AttributeError, OSError, ValueError):
        return
    try:
        os.dup2(devnull_fd, stdout_fd)
    except OSError:
        pass
    finally:
        os.close(devnull_fd)


def main(argv: Sequence[str] | None = None, *, prog: str | None = None) -> int:
    """Parse and execute one Pyruns command without leaking pipe tracebacks."""

    try:
        return _main(argv, prog=prog)
    except BrokenPipeError:
        _silence_broken_pipe()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
