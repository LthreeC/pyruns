"""
Interactive CLI REPL for ``pyr cli``.
"""

from __future__ import annotations

import shlex
import traceback

from pyruns.cli.commands import COMMANDS, cmd_list
from pyruns.cli.display import _BOLD, _DIM, _RESET

_CLI_HELP = f"""
  {_BOLD}Pyruns CLI Commands{_RESET}

  {_BOLD}ls{_RESET} [query]                    List tasks
  {_BOLD}ls{_RESET} --status completed         Filter by status
  {_BOLD}ls{_RESET} -i [query]                 Interactive task browser
  {_BOLD}show{_RESET} <name|#>                 Show detailed task info
  {_BOLD}gen{_RESET} [template]                Generate tasks from YAML template
  {_BOLD}run{_RESET} <name|# ...>              Run one or more tasks
  {_BOLD}run{_RESET} ... --workers 4 --mode process --detach
  {_BOLD}delete{_RESET} <name|# ...> [-y]      Soft-delete tasks to .trash
  {_BOLD}open{_RESET} <name|#> [task]          Open task config or task_info.json
  {_BOLD}export{_RESET} [targets] [--format json]
  {_BOLD}log{_RESET} <name|#>                  View last run log
  {_BOLD}fg{_RESET} <name|#>                   Tail log in real time
  {_BOLD}jobs{_RESET}                          Show running and queued tasks
  {_BOLD}stat{_RESET} [-i]                     System metrics dashboard
  {_BOLD}info{_RESET}                          Show workspace info
  {_BOLD}help{_RESET}                          Show this help
  {_BOLD}exit{_RESET} / {_BOLD}quit{_RESET}                   Exit CLI mode
"""


def run_interactive(tm) -> None:
    """Enter the interactive REPL loop."""
    print(f"\n  {_BOLD}Pyruns CLI{_RESET}  (type 'help' for commands, 'exit' to quit)\n")
    cmd_list(tm, ["--limit", "12"])

    while True:
        try:
            line = input(f"{_BOLD}pyruns>{_RESET} ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n")
            break

        if not line:
            continue

        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()

        cmd_name = parts[0].lower()
        cmd_args = parts[1:]

        if cmd_name in {"exit", "quit", "q"}:
            break

        if cmd_name in {"help", "?"}:
            print(_CLI_HELP)
            continue

        handler = COMMANDS.get(cmd_name)
        if handler is None:
            print(f"  Unknown command: '{cmd_name}'  (type 'help' for available commands)")
            continue

        tm.refresh_from_disk(check_all=True)
        try:
            handler(tm, cmd_args)
        except Exception as exc:
            print(f"  {_DIM}Command failed: {type(exc).__name__}: {exc}{_RESET}")
            traceback.print_exc()
