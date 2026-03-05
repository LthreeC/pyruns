"""
Interactive CLI REPL — ``pyr cli`` enters this loop.

Provides a ``pyruns>`` prompt where users can type commands interactively.
On entry, the current task list is displayed automatically.
"""
import shlex

from pyruns.cli.commands import COMMANDS, cmd_list
from pyruns.cli.display import _BOLD, _RESET, _DIM


_CLI_HELP = f"""
  {_BOLD}Pyruns CLI Commands{_RESET}

  {_BOLD}ls{_RESET} [query]            List tasks (with optional filter)
  {_BOLD}gen{_RESET} [template]        Generate tasks from YAML template
  {_BOLD}run{_RESET} <name|#>          Run task(s) by name or index
  {_BOLD}delete{_RESET} <name|#>       Soft-delete task(s) to .trash
  {_BOLD}jobs{_RESET}                  Show running/queued tasks
  {_BOLD}fg{_RESET} <%N|name|#>        Tail task log (Ctrl+C to detach)
  {_BOLD}help{_RESET}                  Show this help
  {_BOLD}exit{_RESET} / {_BOLD}quit{_RESET}           Exit CLI mode
"""


def run_interactive(tm) -> None:
    """Enter the interactive REPL loop."""
    print(f"\n  {_BOLD}Pyruns CLI{_RESET}  (type 'help' for commands, 'exit' to quit)\n")

    # Show task list on entry
    cmd_list(tm)

    while True:
        try:
            line = input(f"{_BOLD}pyruns>{_RESET} ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n")
            break

        if not line:
            continue

        # Parse command + args
        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()

        cmd_name = parts[0].lower()
        cmd_args = parts[1:]

        if cmd_name in ("exit", "quit", "q"):
            break

        if cmd_name in ("help", "?"):
            print(_CLI_HELP)
            continue

        handler = COMMANDS.get(cmd_name)
        if handler:
            try:
                handler(tm, cmd_args)
            except Exception as e:
                print(f"  Error: {e}")
        else:
            print(f"  Unknown command: '{cmd_name}'  (type 'help' for available commands)")
