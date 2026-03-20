"""
CLI commands for listing, inspecting, running, deleting, and exporting tasks.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import sys
import tempfile
import time
from typing import Any

import psutil

from pyruns._config import (
    CONFIG_DEFAULT_FILENAME,
    RUN_LOGS_DIR,
    TASK_INFO_FILENAME,
    TASK_KIND_CONFIG,
)
from pyruns.cli.display import (
    _BOLD,
    _DIM,
    _RESET,
    _STATUS_STYLES,
    _colored,
    _get_terminal_width,
    print_jobs,
    print_task_detail,
    print_task_table,
)
from pyruns.core.report import build_export_csv, build_export_json, export_timestamp
from pyruns.core.system_metrics import SystemMonitor
from pyruns.core.task_generator import TaskGenerator
from pyruns.utils import get_logger
from pyruns.utils.batch_utils import generate_batch_configs
from pyruns.utils.config_utils import load_yaml, preview_config_line
from pyruns.utils.events import log_emitter
from pyruns.utils.info_io import load_task_info
from pyruns.utils.sort_utils import filter_tasks, task_sort_key
from pyruns.utils.task_files import resolve_task_config_file

logger = get_logger(__name__)

_VALID_STATUSES = {"pending", "queued", "running", "completed", "failed"}


def _sorted_tasks(tm) -> list[dict[str, Any]]:
    """Return tasks sorted identically to the UI Manager page."""
    valid = [task for task in tm.tasks if task is not None]
    return sorted(valid, key=task_sort_key, reverse=True)


def _consume_flag(args: list[str], *names: str) -> tuple[bool, list[str]]:
    found = False
    remaining: list[str] = []
    for arg in args:
        if arg in names:
            found = True
            continue
        remaining.append(arg)
    return found, remaining


def _consume_option(args: list[str], *names: str) -> tuple[str | None, list[str]]:
    remaining: list[str] = []
    index = 0
    value: str | None = None

    while index < len(args):
        arg = args[index]
        matched = next((name for name in names if arg == name or arg.startswith(f"{name}=")), None)
        if matched is None:
            remaining.append(arg)
            index += 1
            continue

        if "=" in arg:
            value = arg.split("=", 1)[1]
            index += 1
            continue

        if index + 1 >= len(args):
            print(f"  Missing value for {matched}")
            return None, remaining

        value = args[index + 1]
        index += 2

    return value, remaining


def _consume_multi_option(args: list[str], *names: str) -> tuple[list[str], list[str]]:
    remaining: list[str] = []
    values: list[str] = []
    index = 0

    while index < len(args):
        arg = args[index]
        matched = next((name for name in names if arg == name or arg.startswith(f"{name}=")), None)
        if matched is None:
            remaining.append(arg)
            index += 1
            continue

        if "=" in arg:
            values.extend(part.strip() for part in arg.split("=", 1)[1].split(",") if part.strip())
            index += 1
            continue

        if index + 1 >= len(args):
            print(f"  Missing value for {matched}")
            return values, remaining

        values.extend(part.strip() for part in args[index + 1].split(",") if part.strip())
        index += 2

    return values, remaining


def _parse_limit(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        print(f"  Invalid limit: {raw}")
        return None
    if value <= 0:
        print("  Limit must be greater than 0.")
        return None
    return value


def _parse_workers(raw: str | None) -> int:
    if raw is None:
        return 1
    try:
        value = int(raw)
    except ValueError:
        print(f"  Invalid workers value: {raw}")
        return 1
    if value <= 0:
        print("  Workers must be greater than 0. Falling back to 1.")
        return 1
    return value


def _normalize_mode(raw: str | None) -> str:
    if not raw:
        return "thread"
    mode = raw.strip().lower()
    if mode in {"thread", "process"}:
        return mode
    print(f"  Unknown mode '{raw}'. Falling back to thread.")
    return "thread"


def _normalize_status_filters(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        status = value.strip().lower()
        if not status:
            continue
        if status not in _VALID_STATUSES:
            print(f"  Unknown status filter: {value}")
            continue
        normalized.append(status)
    return normalized


def _apply_status_filter(tasks: list[dict[str, Any]], statuses: list[str]) -> list[dict[str, Any]]:
    if not statuses:
        return tasks
    allowed = set(statuses)
    return [task for task in tasks if (task.get("status") or "pending") in allowed]


def _refresh_tasks(tm) -> list[dict[str, Any]]:
    tm.refresh_from_disk(check_all=True)
    return _sorted_tasks(tm)


def _get_git_editor() -> str:
    """Resolve an editor command with Git-like precedence."""
    editor = os.environ.get("GIT_EDITOR")
    if editor:
        return editor

    try:
        editor = subprocess.check_output(
            ["git", "config", "--get", "core.editor"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if editor:
            return editor
    except Exception:
        pass

    editor = os.environ.get("VISUAL")
    if editor:
        return editor

    editor = os.environ.get("EDITOR")
    if editor:
        return editor

    term_program = os.environ.get("TERM_PROGRAM", "")
    if term_program == "vscode":
        ipc = os.environ.get("VSCODE_IPC_HOOK_CLI", "").lower()
        return "cursor --wait" if "cursor" in ipc else "code --wait"

    return "notepad" if os.name == "nt" else "vi"


def _resolve_targets(tm, args: list[str]) -> list[dict[str, Any]]:
    """Resolve user-provided names or 1-based indices to task dicts."""
    sorted_tasks = _refresh_tasks(tm)
    targets: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for arg in args:
        task = None
        try:
            index = int(arg)
        except ValueError:
            index = None

        if index is not None:
            if 1 <= index <= len(sorted_tasks):
                task = sorted_tasks[index - 1]
            else:
                print(f"  Task index out of range: {arg}")
                continue
        else:
            task = next((item for item in sorted_tasks if item.get("name") == arg), None)
            if task is None:
                matches = [item for item in sorted_tasks if arg.lower() in (item.get("name") or "").lower()]
                if len(matches) == 1:
                    task = matches[0]
                elif len(matches) > 1:
                    names = ", ".join(match["name"] for match in matches)
                    print(f"  Ambiguous name '{arg}', matches: {names}")
                    continue
                else:
                    print(f"  Task not found: '{arg}'")
                    continue

        name = task.get("name")
        if name and name not in seen_names:
            targets.append(task)
            seen_names.add(name)

    return targets


def _resolve_export_tasks(tm, raw_targets: list[str], statuses: list[str], include_all: bool) -> list[dict[str, Any]]:
    if include_all or not raw_targets:
        tasks = _refresh_tasks(tm)
    else:
        tasks = _resolve_targets(tm, raw_targets)
    return _apply_status_filter(tasks, statuses)


def _format_task_names(tasks: list[dict[str, Any]]) -> list[str]:
    return [task["name"] for task in tasks if task.get("name")]


def _print_skipped_running(tasks: list[dict[str, Any]]) -> None:
    for task in tasks:
        print(f"  Skipping '{task['name']}' because it is already {task.get('status')}.")


def cmd_list(tm, args: list[str] | None = None) -> None:
    """List tasks with optional filter and status controls."""
    raw_args = list(args or [])
    interactive, raw_args = _consume_flag(raw_args, "-i", "--interactive")
    status_values, raw_args = _consume_multi_option(raw_args, "-s", "--status")
    limit_raw, raw_args = _consume_option(raw_args, "-n", "--limit")

    if interactive:
        from pyruns.cli.interactive_ls import run_interactive_ls

        run_interactive_ls(tm, query=" ".join(raw_args).strip())
        return

    statuses = _normalize_status_filters(status_values)
    limit = _parse_limit(limit_raw)
    query = " ".join(raw_args).strip()

    tasks = _refresh_tasks(tm)
    tasks = _apply_status_filter(tasks, statuses)
    if query:
        tasks = filter_tasks(tasks, query)
    if limit is not None:
        tasks = tasks[:limit]

    title = "Tasks"
    if statuses:
        title = f"Tasks [{', '.join(statuses)}]"
    print_task_table(tasks, title=title)


def cmd_show(tm, args: list[str] | None = None) -> None:
    """Show detailed task information."""
    raw_args = list(args or [])
    if not raw_args:
        print("  Usage: show <name|index> [name|index ...]")
        return

    targets = _resolve_targets(tm, raw_args)
    if not targets:
        return

    for task in targets:
        print_task_detail(task)


def cmd_generate(tm, args: list[str] | None = None) -> None:
    """Open editor for YAML config and generate tasks on save."""
    raw_args = list(args or [])
    workspace_dir = os.path.dirname(tm.tasks_dir)

    template_path = None
    if raw_args:
        candidate = raw_args[0]
        if os.path.exists(candidate):
            template_path = candidate
        else:
            candidate = os.path.join(workspace_dir, candidate)
            if os.path.exists(candidate):
                template_path = candidate

    if not template_path:
        template_path = os.path.join(workspace_dir, CONFIG_DEFAULT_FILENAME)

    if not os.path.exists(template_path):
        print(f"  Template not found: {template_path}")
        return

    with open(template_path, "r", encoding="utf-8") as handle:
        template_content = handle.read()

    header = (
        "# Pyruns Task Generator\n"
        "# Edit parameters below, then save and close to generate tasks.\n"
        "# Use pipe syntax for batch values: lr: 0.001 | 0.01 | 0.1\n"
        "# Lines starting with # are comments.\n\n"
    )

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yaml",
        prefix="pyruns_gen_",
        delete=False,
        encoding="utf-8",
    ) as handle:
        handle.write(header + template_content)
        temp_path = handle.name

    editor = _get_git_editor()
    print(f"  Waiting for editor '{editor}' to close the file...")

    try:
        command = shlex.split(editor, posix=(os.name != "nt")) + [temp_path]
        subprocess.run(command, check=True)
    except Exception:
        print(f"  Failed to launch editor '{editor}'.")
        os.unlink(temp_path)
        return

    config = load_yaml(temp_path)
    try:
        os.unlink(temp_path)
    except OSError:
        pass

    if not config:
        print("  Empty config; no tasks generated.")
        return

    configs = generate_batch_configs(config)
    total = len(configs)
    for index, cfg in enumerate(configs, 1):
        preview = preview_config_line(cfg, max_items=5)
        prefix = f"[{index}/{total}] " if total > 1 else ""
        print(f"  {prefix}{preview}")

    answer = input(f"\n  Generate {total} task(s)? [Y/n] ").strip().lower()
    if answer and answer not in {"y", "yes"}:
        print("  Cancelled.")
        return

    name_prefix = input("  Task name prefix (blank=auto): ").strip()
    generator = TaskGenerator(root_dir=tm.tasks_dir)
    new_tasks = generator.create_tasks(configs, name_prefix, task_kind=TASK_KIND_CONFIG)
    for task in new_tasks:
        tm.add_task(task)

    print(f"\n  Created {len(new_tasks)} task(s)")
    for task in new_tasks:
        print(f"    - {task['name']}")
    print()


def cmd_run(tm, args: list[str] | None = None) -> None:
    """Run one or more tasks with direct batch controls."""
    raw_args = list(args or [])
    detach, raw_args = _consume_flag(raw_args, "-d", "--detach", "--no-follow")
    workers_raw, raw_args = _consume_option(raw_args, "-w", "--workers")
    mode_raw, raw_args = _consume_option(raw_args, "-m", "--mode")

    if not raw_args:
        print("  Usage: run <name|index> [name|index ...] [--workers N] [--mode thread|process] [--detach]")
        return

    mode = _normalize_mode(mode_raw)
    workers = _parse_workers(workers_raw)
    targets = _resolve_targets(tm, raw_args)
    if not targets:
        return

    runnable = [task for task in targets if task.get("status") not in {"running", "queued"}]
    skipped = [task for task in targets if task.get("status") in {"running", "queued"}]
    if skipped:
        _print_skipped_running(skipped)
    if not runnable:
        print("  No runnable tasks found.")
        return

    if len(runnable) == 1:
        task = runnable[0]
        if workers_raw is not None and workers != 1:
            print("  Note: --workers is ignored when running a single task immediately.")
        print(f"  Starting '{task['name']}'...")
        if mode_raw is None:
            tm.start_task_now(task["name"])
        else:
            tm.start_task_now(task["name"], execution_mode=mode)
        print("  Submitted 1 task.")
        if detach:
            print()
            return
        time.sleep(0.3)
        cmd_fg(tm, [task["name"]])
        return

    task_names = _format_task_names(runnable)
    print(f"\n  {_BOLD}Batch Run{_RESET}  ({len(task_names)} task(s))")
    for name in task_names:
        print(f"    - {name}")
    tm.start_batch_tasks(task_names, execution_mode=mode, max_workers=workers)
    print(f"\n  Submitted {len(task_names)} task(s) in {mode} mode (workers={workers})\n")


def cmd_delete(tm, args: list[str] | None = None) -> None:
    """Soft-delete tasks to .trash with optional non-interactive confirm."""
    raw_args = list(args or [])
    confirmed, raw_args = _consume_flag(raw_args, "-y", "--yes")

    if not raw_args:
        print("  Usage: delete <name|index> [name|index ...] [-y|--yes]")
        return

    targets = _resolve_targets(tm, raw_args)
    if not targets:
        return

    names = _format_task_names(targets)
    print(f"  Will delete: {', '.join(names)}")
    if not confirmed:
        answer = input("  Confirm? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("  Cancelled.")
            return

    tm.delete_tasks(names)
    print(f"\n  {_colored('Deleted', _STATUS_STYLES['failed'])} {len(targets)} task(s)\n")


def cmd_jobs(tm, args: list[str] | None = None) -> None:
    """Show running and queued tasks in Linux-jobs style."""
    _refresh_tasks(tm)
    print_jobs(_sorted_tasks(tm))


def cmd_fg(tm, args: list[str] | None = None) -> None:
    """Tail a task log inline until detached."""
    raw_args = list(args or [])
    if not raw_args:
        print("  Usage: fg <name|index>")
        return

    targets = _resolve_targets(tm, [raw_args[0]])
    if not targets:
        return

    task = targets[0]
    task_dir = task.get("dir")
    task_name = task.get("name", "unknown")

    tm.refresh_from_disk(task_ids=[task_name])
    task_info = load_task_info(task_dir) or {}
    if task_info.get("status") in {"running", "queued"}:
        run_index = task_info.get("run_index", 1)
    else:
        run_index = max(1, len(task_info.get("start_times", [])))

    target_log = os.path.join(task_dir, RUN_LOGS_DIR, f"run{run_index}.log")
    print(f"\n  {_BOLD}== {task_name} =={_RESET}  (Ctrl+C to detach)\n")

    for _ in range(10):
        if os.path.exists(target_log):
            break
        time.sleep(0.1)

    try:
        with open(target_log, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
            if content:
                sys.stdout.write(content.replace("\r\n", "\n"))
                sys.stdout.flush()
    except Exception:
        pass

    log_queue: queue.Queue = queue.Queue()

    def _on_chunk(chunk: str) -> None:
        log_queue.put(chunk)

    log_emitter.subscribe(task_name, _on_chunk)

    try:
        while True:
            try:
                while True:
                    chunk = log_queue.get(timeout=0.2)
                    sys.stdout.write(chunk.replace("\r\n", "\n"))
                    sys.stdout.flush()
            except queue.Empty:
                pass

            task_map = {item.get("name"): item for item in _refresh_tasks(tm)}
            current = task_map.get(task_name)
            if current and current.get("status") not in {"running", "queued"}:
                while not log_queue.empty():
                    sys.stdout.write(log_queue.get_nowait().replace("\r\n", "\n"))
                    sys.stdout.flush()
                status = current.get("status", "unknown")
                style = _STATUS_STYLES.get(status, "")
                print(f"\n\n  {_colored(f'Task {status}', style)}\n")
                break
    except KeyboardInterrupt:
        print(f"\n\n  {_DIM}Detached from {task_name}{_RESET}\n")
    finally:
        log_emitter.unsubscribe(task_name, _on_chunk)


def cmd_log(tm, args: list[str] | None = None) -> None:
    """View log files for a task in the alternate-screen viewer."""
    raw_args = list(args or [])
    if not raw_args:
        print("  Usage: log <name|index>")
        return

    targets = _resolve_targets(tm, [raw_args[0]])
    if not targets:
        return

    from pyruns.cli.interactive_ls import _view_log

    _view_log(targets[0])


def cmd_open(tm, args: list[str] | None = None) -> None:
    """Open a task config or task_info.json in the editor."""
    raw_args = list(args or [])
    if not raw_args:
        print("  Usage: open <name|index> [config|task]")
        return

    targets = _resolve_targets(tm, [raw_args[0]])
    if not targets:
        return

    target = targets[0]
    task_dir = target.get("dir")
    file_type = raw_args[1].lower() if len(raw_args) > 1 else "config"
    target_path = (
        os.path.join(task_dir, TASK_INFO_FILENAME)
        if file_type == "task"
        else os.path.join(task_dir, resolve_task_config_file(target))
    )

    if not os.path.exists(target_path):
        print(f"  File not found: {target_path}")
        return

    editor = _get_git_editor()
    editor_no_wait = editor.replace(" --wait", "").replace(" -w", "")
    print(f"  Opening {os.path.basename(target_path)} ...")
    try:
        command = shlex.split(editor_no_wait, posix=(os.name != "nt")) + [target_path]
        subprocess.Popen(command)
    except Exception as exc:
        print(f"  Failed to open editor: {exc}")


def cmd_export(tm, args: list[str] | None = None) -> None:
    """Export tasks to CSV or JSON."""
    raw_args = list(args or [])
    include_all, raw_args = _consume_flag(raw_args, "-a", "--all")
    status_values, raw_args = _consume_multi_option(raw_args, "-s", "--status")
    format_raw, raw_args = _consume_option(raw_args, "-f", "--format")
    output_raw, raw_args = _consume_option(raw_args, "-o", "--output")

    statuses = _normalize_status_filters(status_values)
    export_tasks = _resolve_export_tasks(tm, raw_args, statuses, include_all)
    if not export_tasks:
        print("  No tasks matched for export.")
        return

    export_format = "json" if (format_raw or "").lower() == "json" else "csv"
    output_path = output_raw or f"pyruns_export_{export_timestamp()}.{export_format}"
    content = build_export_json(export_tasks) if export_format == "json" else build_export_csv(export_tasks)

    if not content:
        print("  No exportable data found.")
        return

    with open(output_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)

    print(f"  Exported {len(export_tasks)} task(s) to {os.path.abspath(output_path)}")


def cmd_stat(tm, args: list[str] | None = None) -> None:
    """Show system metrics once or in live-refresh mode."""
    raw_args = list(args or [])
    interactive, _ = _consume_flag(raw_args, "-i", "--interactive")
    if interactive:
        _stat_interactive()
        return
    _stat_once()


def cmd_info(tm, args: list[str] | None = None) -> None:
    """Show current workspace and task summary information."""
    print(f"\n  {_BOLD}Workspace Info{_RESET}")
    separator = f"  {'-' * max(8, _get_terminal_width() - 4)}"
    print(separator)

    tasks_dir = getattr(tm, "tasks_dir", None)
    workspace_dir = os.path.dirname(tasks_dir) if tasks_dir else None
    if workspace_dir:
        print(f"  Workspace:  {workspace_dir}")
        script_info_path = os.path.join(workspace_dir, "script_info.json")
        if os.path.exists(script_info_path):
            try:
                with open(script_info_path, "r", encoding="utf-8") as handle:
                    info = json.load(handle)
                print(f"  Script:     {info.get('script_name', 'N/A')}")
                script_path = info.get("script_path", "")
                if script_path:
                    print(f"  Path:       {script_path}")
            except Exception:
                pass
    else:
        print(f"  {_DIM}No workspace detected.{_RESET}")

    tasks = _refresh_tasks(tm)
    print(f"  Tasks:      {len(tasks)} total")
    counts = {status: 0 for status in sorted(_VALID_STATUSES)}
    for task in tasks:
        counts[(task.get("status") or "pending")] += 1
    print(
        "  Statuses:   "
        f"pending={counts['pending']}, queued={counts['queued']}, "
        f"running={counts['running']}, completed={counts['completed']}, failed={counts['failed']}"
    )
    print(separator)
    print()


def _stat_once() -> None:
    """Print one snapshot of CPU, RAM, and GPU usage."""
    monitor = SystemMonitor()
    metrics = monitor.sample()
    cpu = metrics.get("cpu_percent", 0)
    memory_percent = metrics.get("mem_percent", 0)
    memory_info = psutil.virtual_memory()
    gpus = metrics.get("gpus", [])

    separator = f"  {'-' * max(8, _get_terminal_width() - 4)}"
    print(f"\n  {_BOLD}System Metrics{_RESET}")
    print(separator)
    print(f"  CPU:  {_bar(cpu)}")
    print(
        "  RAM:  "
        f"{_bar(memory_percent)}  ({memory_info.used // (1024 ** 2):,} / "
        f"{memory_info.total // (1024 ** 2):,} MB)"
    )

    if gpus:
        print()
        for gpu in gpus:
            util = gpu.get("util", 0)
            mem_used = gpu.get("mem_used", 0)
            mem_total = max(gpu.get("mem_total", 1), 1)
            mem_percent = (mem_used / mem_total) * 100
            index = gpu.get("index", "?")
            print(
                f"  GPU {index}:  {_bar(util)}  "
                f"VRAM {_bar(mem_percent)}  ({int(mem_used)} / {int(mem_total)} MB)"
            )
    else:
        print(f"\n  {_DIM}No GPUs detected.{_RESET}")

    print(separator)
    print()


def _stat_interactive() -> None:
    """Continuously refresh system metrics until Ctrl+C."""
    monitor = SystemMonitor()
    sys.stdout.write("\033[?1049h")
    sys.stdout.flush()

    try:
        while True:
            metrics = monitor.sample()
            cpu = metrics.get("cpu_percent", 0)
            memory_percent = metrics.get("mem_percent", 0)
            memory_info = psutil.virtual_memory()
            gpus = metrics.get("gpus", [])
            separator = f"  {'-' * max(8, _get_terminal_width() - 4)}"

            output = ["\033[2J\033[H"]
            output.append(
                f"\n  {_BOLD}System Metrics{_RESET}  "
                f"{_DIM}(refreshing every 1s, press Ctrl+C to exit){_RESET}\n"
            )
            output.append(separator + "\n")
            output.append(f"  CPU:  {_bar(cpu)}\n")
            output.append(
                "  RAM:  "
                f"{_bar(memory_percent)}  ({memory_info.used // (1024 ** 2):,} / "
                f"{memory_info.total // (1024 ** 2):,} MB)\n"
            )

            if gpus:
                output.append("\n")
                for gpu in gpus:
                    util = gpu.get("util", 0)
                    mem_used = gpu.get("mem_used", 0)
                    mem_total = max(gpu.get("mem_total", 1), 1)
                    mem_percent = (mem_used / mem_total) * 100
                    index = gpu.get("index", "?")
                    output.append(
                        f"  GPU {index}:  {_bar(util)}  "
                        f"VRAM {_bar(mem_percent)}  ({int(mem_used)} / {int(mem_total)} MB)\n"
                    )
            else:
                output.append(f"\n  {_DIM}No GPUs detected.{_RESET}\n")

            output.append(separator + "\n")
            sys.stdout.write("".join(output))
            sys.stdout.flush()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?1049l")
        sys.stdout.flush()
        print(f"  {_DIM}Exited stat view.{_RESET}\n")


def _bar(percent: float, width: int = 30) -> str:
    """Render a colored progress bar."""
    clamped = max(0.0, min(100.0, float(percent)))
    filled = int(width * clamped / 100)
    empty = width - filled
    if clamped < 60:
        color = "\033[32m"
    elif clamped < 85:
        color = "\033[33m"
    else:
        color = "\033[31m"
    return f"{color}{'#' * filled}{_DIM}{'.' * empty}{_RESET}  {color}{clamped:5.1f}%{_RESET}"


COMMANDS = {
    "ls": cmd_list,
    "list": cmd_list,
    "show": cmd_show,
    "inspect": cmd_show,
    "gen": cmd_generate,
    "generate": cmd_generate,
    "gentask": cmd_generate,
    "run": cmd_run,
    "delete": cmd_delete,
    "del": cmd_delete,
    "rm": cmd_delete,
    "open": cmd_open,
    "export": cmd_export,
    "log": cmd_log,
    "fg": cmd_fg,
    "jobs": cmd_jobs,
    "stat": cmd_stat,
    "status": cmd_stat,
    "info": cmd_info,
}
