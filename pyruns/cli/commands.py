"""
CLI commands — ls, gen, run, delete, jobs, fg.

Each command is a plain function that takes a ``TaskManager`` (and optional args)
and prints results to stdout.  All task lifecycle logic is delegated to
``core.task_manager`` and ``core.task_generator`` so CLI and UI share the same
business logic.
"""
import os
import sys
import time
import tempfile
import subprocess
from typing import List, Optional

from pyruns._config import CONFIG_DEFAULT_FILENAME, TASKS_DIR, RUN_LOGS_DIR
from pyruns.utils.sort_utils import task_sort_key, filter_tasks
from pyruns.utils.config_utils import load_yaml, preview_config_line
from pyruns.utils.info_io import resolve_log_path
from pyruns.cli.display import (
    print_task_table, print_jobs, print_task_detail,
    _BOLD, _RESET, _DIM, _colored, _STATUS_STYLES,
)
from pyruns.utils import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════

def _sorted_tasks(tm) -> list:
    """Return tasks sorted identically to the UI Manager page."""
    return sorted(tm.tasks[:], key=task_sort_key, reverse=True)


def _resolve_targets(tm, args: List[str]) -> list:
    """Resolve user-provided names or 1-based indices to task dicts."""
    sorted_tasks = _sorted_tasks(tm)
    targets = []
    for arg in args:
        # Try as 1-based index
        try:
            idx = int(arg)
            if 1 <= idx <= len(sorted_tasks):
                targets.append(sorted_tasks[idx - 1])
                continue
        except ValueError:
            pass
        # Try as name (substring match)
        for t in sorted_tasks:
            if t.get("name") == arg:
                targets.append(t)
                break
        else:
            # Fuzzy: partial name match
            matches = [t for t in sorted_tasks if arg.lower() in t.get("name", "").lower()]
            if len(matches) == 1:
                targets.append(matches[0])
            elif len(matches) > 1:
                print(f"  Ambiguous name '{arg}', matches: {[m['name'] for m in matches]}")
            else:
                print(f"  Task not found: '{arg}'")
    return targets


# ═══════════════════════════════════════════════════════════════
#  Commands
# ═══════════════════════════════════════════════════════════════


def cmd_list(tm, args: List[str] = None) -> None:
    """``ls [query]`` — list tasks with optional filter."""
    query = " ".join(args) if args else ""
    tasks = _sorted_tasks(tm)
    if query:
        tasks = filter_tasks(tasks, query)
    print_task_table(tasks)


def cmd_generate(tm, args: List[str] = None) -> None:
    """``gen [template_path]`` — open editor for YAML config, create tasks on save.

    The template is either a user-provided path or the workspace's
    ``config_default.yaml``.  After the user edits and saves the temp file,
    it is parsed and tasks are generated (batch syntax ``|`` supported).
    """
    from pyruns.core.task_generator import TaskGenerator
    from pyruns.utils.batch_utils import generate_batch_configs

    workspace_dir = os.path.dirname(tm.root_dir)  # e.g. _pyruns_/main

    # Resolve template
    template_path = None
    if args:
        candidate = args[0]
        if os.path.exists(candidate):
            template_path = candidate
        else:
            # Try relative to workspace
            candidate = os.path.join(workspace_dir, candidate)
            if os.path.exists(candidate):
                template_path = candidate
    if not template_path:
        template_path = os.path.join(workspace_dir, CONFIG_DEFAULT_FILENAME)

    if not os.path.exists(template_path):
        print(f"  Template not found: {template_path}")
        print(f"  Hint: place a {CONFIG_DEFAULT_FILENAME} in your run root.")
        return

    # Read template content
    with open(template_path, "r", encoding="utf-8") as f:
        template_content = f.read()

    # Write to temp file for editing
    header = (
        "# ── Pyruns Task Generator ──────────────────────────────────\n"
        "# Edit parameters below, then SAVE and CLOSE to generate tasks.\n"
        "# Use pipe syntax for batch: lr: 0.001 | 0.01 | 0.1\n"
        "# Lines starting with # are comments.\n"
        "# ──────────────────────────────────────────────────────────\n\n"
    )

    suffix = ".yaml"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, prefix="pyruns_gen_",
        delete=False, encoding="utf-8",
    ) as tmp:
        tmp.write(header + template_content)
        tmp_path = tmp.name

    # Open editor
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        editor = "notepad" if os.name == "nt" else "vi"

    try:
        print(f"  Opening {editor}... (save & close to generate)")
        subprocess.run([editor, tmp_path], check=True)
    except Exception as e:
        print(f"  Failed to open editor: {e}")
        return
    finally:
        pass

    # Parse the edited file
    config = load_yaml(tmp_path)
    try:
        os.unlink(tmp_path)
    except OSError:
        pass

    if not config:
        print("  Empty config — no tasks generated.")
        return

    # Generate batch configs
    configs = generate_batch_configs(config)
    n = len(configs)

    # Preview
    for i, cfg in enumerate(configs):
        preview = preview_config_line(cfg, max_items=5)
        tag = f"[{i+1}/{n}]" if n > 1 else ""
        print(f"  {tag} {preview}")

    # Confirm
    answer = input(f"\n  Generate {n} task(s)? [Y/n] ").strip().lower()
    if answer and answer not in ("y", "yes"):
        print("  Cancelled.")
        return

    # Ask for task name prefix
    name_prefix = input("  Task name prefix (blank=auto): ").strip()

    # Create tasks
    gen = TaskGenerator(root_dir=tm.root_dir)
    new_tasks = gen.create_tasks(configs, name_prefix)
    for t in new_tasks:
        tm.add_task(t)

    print(f"\n  {_colored(f'✔ Created {len(new_tasks)} task(s)', _STATUS_STYLES['completed'])}")
    for t in new_tasks:
        print(f"    • {t['name']}")
    print()


def cmd_run(tm, args: List[str] = None) -> None:
    """``run <name|index> [...]`` — run tasks by name or list index."""
    if not args:
        print("  Usage: run <name|index> [name|index ...]")
        return

    targets = _resolve_targets(tm, args)
    if not targets:
        return

    for t in targets:
        status = t.get("status", "pending")
        if status == "running":
            print(f"  '{t['name']}' is already running — skipping.")
            continue
        print(f"  Starting '{t['name']}'...")
        tm.start_task_now(t["name"])

    print(f"\n  {_colored('✔ Submitted', _STATUS_STYLES['running'])} {len(targets)} task(s)\n")


def cmd_delete(tm, args: List[str] = None) -> None:
    """``delete <name|index> [...]`` — soft-delete tasks to .trash."""
    if not args:
        print("  Usage: delete <name|index> [name|index ...]")
        return

    targets = _resolve_targets(tm, args)
    if not targets:
        return

    # Confirm
    names = [t["name"] for t in targets]
    print(f"  Will delete: {', '.join(names)}")
    answer = input("  Confirm? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("  Cancelled.")
        return

    task_ids = [t["name"] for t in targets]
    tm.delete_tasks(task_ids)
    print(f"\n  {_colored('✔ Deleted', _STATUS_STYLES['failed'])} {len(targets)} task(s)\n")


def cmd_jobs(tm, args: List[str] = None) -> None:
    """``jobs`` — show running/queued tasks in Linux jobs style."""
    tm.refresh_from_disk(check_all=True)
    tasks = _sorted_tasks(tm)
    print_jobs(tasks)


def cmd_fg(tm, args: List[str] = None) -> None:
    """``fg <%N|name|index>`` — tail a task's log file (Ctrl+C to exit)."""
    if not args:
        print("  Usage: fg <%N|name|index>")
        print("  Example: fg %1  (first job from 'jobs' list)")
        return

    ref = args[0]

    # Parse %N syntax (refers to jobs list ordering)
    if ref.startswith("%"):
        try:
            job_idx = int(ref[1:])
        except ValueError:
            print(f"  Invalid job reference: {ref}")
            return
        active = [t for t in _sorted_tasks(tm) if t.get("status") in ("running", "queued")]
        if not active:
            print("  No active jobs.")
            return
        if job_idx < 1 or job_idx > len(active):
            print(f"  Job index out of range (1-{len(active)})")
            return
        target = active[job_idx - 1]
    else:
        targets = _resolve_targets(tm, [ref])
        if not targets:
            return
        target = targets[0]

    task_dir = target.get("dir")
    task_name = target.get("name", "unknown")

    log_path = resolve_log_path(task_dir)
    if not log_path or not os.path.exists(log_path):
        print(f"  No log file found for '{task_name}'")
        return

    # Tail the log
    print(f"\n  {_BOLD}── {task_name} ──{_RESET}  (Ctrl+C to exit)\n")

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            # Print existing content
            content = f.read()
            if content:
                sys.stdout.write(content)
                sys.stdout.flush()

            # Follow new content
            while True:
                line = f.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    # Check if task is still running
                    tm.refresh_from_disk(task_ids=[task_name])
                    found = None
                    for t in tm.tasks:
                        if t["name"] == task_name:
                            found = t
                            break
                    if found and found.get("status") not in ("running", "queued"):
                        # Task finished — read any remaining and exit
                        remaining = f.read()
                        if remaining:
                            sys.stdout.write(remaining)
                            sys.stdout.flush()
                        status = found.get("status", "unknown")
                        style = _STATUS_STYLES.get(status, "")
                        print(f"\n\n  {_colored(f'Task {status}', style)}\n")
                        break
                    time.sleep(0.3)
    except KeyboardInterrupt:
        print(f"\n\n  {_DIM}Detached from {task_name}{_RESET}\n")


# ═══════════════════════════════════════════════════════════════
#  Command registry
# ═══════════════════════════════════════════════════════════════

COMMANDS = {
    "ls":       cmd_list,
    "list":     cmd_list,
    "gen":      cmd_generate,
    "generate": cmd_generate,
    "run":      cmd_run,
    "delete":   cmd_delete,
    "del":      cmd_delete,
    "rm":       cmd_delete,
    "jobs":     cmd_jobs,
    "fg":       cmd_fg,
}
