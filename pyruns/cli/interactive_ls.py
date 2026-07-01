"""
Interactive terminal task browser for ``pyr ls -i``.
"""

from __future__ import annotations

import os
import queue
import select
import shutil
import sys
import time

from pyruns.cli.console import write_console_text
from pyruns.cli.display import _BOLD, _DIM, _RESET, _get_terminal_width, _status_str, _truncate
from pyruns.core.report import build_export_csv, build_export_json, export_timestamp
from pyruns.utils.events import log_emitter
from pyruns.utils.info_io import get_log_options, load_task_info, resolve_log_path
from pyruns.utils.sort_utils import filter_tasks

if os.name == "nt":
    import msvcrt

    def _flush_input() -> None:
        while msvcrt.kbhit():
            msvcrt.getch()

    def getch() -> str:
        chunk = msvcrt.getch()
        if chunk in (b"\x00", b"\xe0"):
            code = msvcrt.getch()
            if code == b"\x48":
                return "up"
            if code == b"\x50":
                return "down"
            return ""
        try:
            return chunk.decode("utf-8", errors="ignore")
        except Exception:
            return ""

else:
    import termios
    import tty

    def _flush_input() -> None:
        file_descriptor = sys.stdin.fileno()
        while select.select([file_descriptor], [], [], 0)[0]:
            os.read(file_descriptor, 1024)

    def getch() -> str:
        file_descriptor = sys.stdin.fileno()
        old_settings = termios.tcgetattr(file_descriptor)
        try:
            tty.setraw(file_descriptor)
            chunk = sys.stdin.read(1)
            if chunk == "\x1b":
                next_char = sys.stdin.read(1)
                if next_char == "[":
                    arrow = sys.stdin.read(1)
                    if arrow == "A":
                        return "up"
                    if arrow == "B":
                        return "down"
            return chunk
        finally:
            termios.tcsetattr(file_descriptor, termios.TCSADRAIN, old_settings)


def _enter_alt() -> None:
    sys.stdout.write("\033[?1049h")
    sys.stdout.flush()
    time.sleep(0.05)
    _flush_input()


def _leave_alt() -> None:
    sys.stdout.write("\033[?1049l")
    sys.stdout.flush()


def run_interactive_ls(tm, query: str = "") -> None:
    from pyruns.cli.commands import _sorted_tasks, cmd_open

    cursor = 0
    selected: set[str] = set()
    in_filter = False
    _enter_alt()

    try:
        while True:
            tm.refresh_from_disk(check_all=True)
            all_tasks = _sorted_tasks(tm)
            tasks = filter_tasks(all_tasks, query) if query else all_tasks

            if not tasks:
                cursor = 0
            else:
                cursor = min(cursor, len(tasks) - 1)

            output = ["\033[2J\033[H"]
            terminal_width = _get_terminal_width()
            selected_count = len(selected)

            title = f"  {_BOLD}Pyruns Interactive View{_RESET}  [ {len(tasks)} tasks"
            if selected_count:
                title += f" | {selected_count} selected"
            title += " ]"
            if query:
                title += f"  {_DIM}(filter: '{query}'){_RESET}"
            output.append(f"\n{title}\n\n")

            index_width = max(3, len(str(len(tasks) or 1)))
            status_width = 13
            time_width = 19
            name_width = max(12, terminal_width - index_width - status_width - time_width - 16)

            output.append(f"       {'#':<{index_width}}  {'Status':<{status_width}} {'Name':<{name_width}}  {'Created':<{time_width}}\n")
            output.append(f"  {'-' * max(8, terminal_width - 4)}\n")

            if not tasks:
                output.append(f"  {_DIM}No tasks found.{_RESET}\n")
            else:
                window = 20
                start = max(0, cursor - window // 2)
                end = min(len(tasks), start + window)
                if end - start < window and len(tasks) > window:
                    start = max(0, end - window)

                for index in range(start, end):
                    task = tasks[index]
                    is_current = index == cursor
                    task_name = task.get("name") or ""
                    checked = task_name in selected
                    checkbox = f"\033[33mx\033[0m" if checked else " "
                    name = _truncate(task_name or "unnamed", name_width)
                    created = (task.get("created_at") or "")[:time_width]
                    runs = len(task.get("start_times") or [])
                    status_cell = _status_str(task.get("status") or "pending", runs)
                    pointer = f"{_BOLD}\033[36m>\033[0m" if is_current else " "
                    if is_current:
                        output.append(
                            f"{pointer} [{checkbox}] {index + 1:<{index_width}}  "
                            f"{status_cell} {_BOLD}{name:<{name_width}}{_RESET}  {_DIM}{created}{_RESET}\n"
                        )
                    else:
                        output.append(
                            f"{pointer} [{checkbox}] {index + 1:<{index_width}}  "
                            f"{status_cell} {name:<{name_width}}  {_DIM}{created}{_RESET}\n"
                        )

            output.append(f"\n  {'-' * max(8, terminal_width - 4)}\n")

            if in_filter:
                output.append(f"  {_BOLD}Filter:{_RESET} type query (empty=clear)\n")
                write_console_text("".join(output))
                sys.stdout.flush()
                try:
                    query = input(f"  {_BOLD}Search: {_RESET}").strip()
                except KeyboardInterrupt:
                    pass
                in_filter = False
                cursor = 0
                _flush_input()
                continue

            output.append(
                f"  {_BOLD}KEYS:{_RESET} [c] Select  [a] All  [r] Run  [b] Batch  "
                f"[d] Delete  [o] Open  [l] Log  [e] Env  [x] Export  [f] Filter  [q] Quit\n"
            )
            sys.stdout.write("".join(output))
            sys.stdout.flush()

            key = getch()
            if key in ("up", "k"):
                cursor = max(0, cursor - 1)
                continue
            if key in ("down", "j") and tasks:
                cursor = min(len(tasks) - 1, cursor + 1)
                continue
            if key in ("q", "\x03"):
                break
            if key in ("f", "/"):
                in_filter = True
                continue

            if key == "c" and tasks:
                task_name = tasks[cursor].get("name", "")
                if task_name in selected:
                    selected.discard(task_name)
                else:
                    selected.add(task_name)
                continue

            if key == "a":
                visible_names = {task.get("name", "") for task in tasks}
                if visible_names and selected >= visible_names:
                    selected.clear()
                else:
                    selected = visible_names.copy()
                continue

            if key == "r" and tasks:
                task = tasks[cursor]
                if task.get("status") not in {"running", "queued"}:
                    tm.start_task_now(task["name"])
                continue

            if key == "b" and selected:
                _leave_alt()
                _batch_run(tm, tasks, selected)
                selected.clear()
                _enter_alt()
                continue

            if key == "d" and tasks:
                targets = [task for task in tasks if task.get("name") in selected] if selected else [tasks[cursor]]
                _leave_alt()
                _delete_tasks(tm, targets)
                selected.clear()
                _enter_alt()
                continue

            if key == "o" and tasks:
                cmd_open(tm, [tasks[cursor]["name"]])
                continue

            if key == "l" and tasks:
                _leave_alt()
                _view_log(tasks[cursor])
                _enter_alt()
                continue

            if key == "e" and tasks:
                _leave_alt()
                _edit_env(tm, tasks[cursor])
                _enter_alt()
                continue

            if key == "x":
                export_tasks = [task for task in tasks if task.get("name") in selected] if selected else tasks
                _leave_alt()
                _do_export(export_tasks)
                input(f"\n  {_DIM}Press Enter to return...{_RESET}")
                _enter_alt()
    finally:
        _leave_alt()
        print(f"  {_DIM}Exited interactive mode.{_RESET}\n")


def _batch_run(tm, tasks: list[dict[str, object]], selected: set[str]) -> None:
    """Prompt for batch settings and run selected tasks."""
    names = [
        task["name"]
        for task in tasks
        if task.get("name") in selected and task.get("status") not in {"running", "queued"}
    ]
    if not names:
        print(f"\n  {_DIM}No runnable tasks selected.{_RESET}")
        input(f"  {_DIM}Press Enter to return...{_RESET}")
        return

    print(f"\n  {_BOLD}Batch Run{_RESET}  ({len(names)} task(s))")
    for name in names:
        print(f"    - {name}")

    try:
        workers_raw = input("\n  Max workers (default=1): ").strip()
        max_workers = int(workers_raw) if workers_raw else 1
        mode = input("  Mode [thread/process] (default=thread): ").strip().lower()
        if mode not in {"process"}:
            mode = "thread"
        tm.start_batch_tasks(names, execution_mode=mode, max_workers=max_workers)
        print(f"\n  Submitted {len(names)} task(s) in {mode} mode (workers={max_workers})")
    except KeyboardInterrupt:
        print("\n  Cancelled.")

    input(f"  {_DIM}Press Enter to return...{_RESET}")


def _delete_tasks(tm, targets: list[dict[str, object]]) -> None:
    """Delete tasks with confirmation."""
    names = [task["name"] for task in targets]
    print(f"\n  {_BOLD}Delete Tasks{_RESET}")
    for name in names:
        print(f"    - {name}")

    try:
        answer = input(f"\n  Delete {len(names)} task(s)? [y/N] ").strip().lower()
        if answer in {"y", "yes"}:
            tm.delete_tasks(names)
            print(f"  Deleted {len(names)} task(s)")
        else:
            print("  Cancelled.")
    except KeyboardInterrupt:
        print("\n  Cancelled.")

    input(f"  {_DIM}Press Enter to return...{_RESET}")


def _view_log(task: dict[str, object]) -> None:
    """View log files in alt-screen with next/prev navigation."""
    task_dir = task.get("dir")
    task_name = task.get("name", "unknown")
    options = get_log_options(task_dir)
    if not options:
        print(f"\n  {_DIM}No log files for '{task_name}'{_RESET}")
        input(f"  {_DIM}Press Enter to return...{_RESET}")
        return

    keys = list(options.keys())
    default_path = resolve_log_path(task_dir)
    current_index = 0
    if default_path:
        for index, key in enumerate(keys):
            if options[key] == default_path:
                current_index = index
                break

    live_queue: queue.Queue[str] = queue.Queue()

    def _on_live_chunk(chunk: str) -> None:
        live_queue.put(chunk)

    sys.stdout.write("\033[?1049h")
    sys.stdout.flush()
    needs_redraw = True

    try:
        log_emitter.subscribe(task_name, _on_live_chunk)
        while True:
            log_name = keys[current_index]
            log_path = options[log_name]
            terminal_width = _get_terminal_width()
            separator = f"  {'-' * max(8, terminal_width - 4)}"
            nav = f"  {_BOLD}KEYS:{_RESET} [n] Next log  [p] Prev log  [q] Back"

            if needs_redraw:
                options = get_log_options(task_dir) or {log_name: log_path}
                keys = list(options.keys())
                current_index = min(current_index, len(keys) - 1)
                log_name = keys[current_index]
                log_path = options[log_name]

                output = ["\033[2J\033[H"]
                output.append(f"\n  {_BOLD}== {task_name} =={_RESET}  {log_name}  ({current_index + 1}/{len(keys)})\n")
                output.append(separator + "\n")
                try:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                        content = handle.read()
                    if content:
                        lines = content.splitlines()
                        terminal_height = shutil.get_terminal_size().lines - 8
                        if len(lines) > terminal_height:
                            output.append(f"  {_DIM}... ({len(lines) - terminal_height} lines omitted) ...{_RESET}\n\n")
                            lines = lines[-terminal_height:]
                        for line in lines:
                            output.append(f"  {line}\n")
                    else:
                        output.append(f"  {_DIM}(empty - waiting for output...){_RESET}\n")
                except Exception as exc:
                    output.append(f"  Error: {exc}\n")

                output.append(f"\n{separator}\n")
                output.append(nav + "\n")
                sys.stdout.write("".join(output))
                sys.stdout.flush()
                needs_redraw = False

                while not live_queue.empty():
                    try:
                        live_queue.get_nowait()
                    except queue.Empty:
                        break

            try:
                while True:
                    chunk = live_queue.get_nowait()
                    sys.stdout.write("\033[3A")
                    sys.stdout.write("\033[J")
                    for line in chunk.replace("\r\n", "\n").splitlines():
                        write_console_text(f"  {line}\n")
                    sys.stdout.write(f"\n{separator}\n")
                    sys.stdout.write(nav + "\n")
                    sys.stdout.flush()
            except queue.Empty:
                pass

            if os.name == "nt":
                if msvcrt.kbhit():
                    key = getch()
                else:
                    time.sleep(0.05)
                    continue
            else:
                if not select.select([sys.stdin], [], [], 0.05)[0]:
                    continue
                key = getch()

            if key in ("q", "\x03"):
                break
            if key == "n":
                current_index = (current_index + 1) % len(keys)
                needs_redraw = True
            elif key == "p":
                current_index = (current_index - 1) % len(keys)
                needs_redraw = True
    finally:
        log_emitter.unsubscribe(task_name, _on_live_chunk)
        sys.stdout.write("\033[?1049l")
        sys.stdout.flush()


def _edit_env(tm, task: dict[str, object]) -> None:
    """Edit task env values via task_info.json."""
    print(f"\n  {_BOLD}Environment Variables for '{task['name']}'{_RESET}")
    print(f"  {_DIM}Type KEY=VALUE to set, or KEY to delete. Blank to cancel.{_RESET}\n")

    info = load_task_info(task["dir"]) or {}
    env = info.get("env", {}) or {}
    if not env:
        print(f"  {_DIM}(No custom variables){_RESET}")
    else:
        for key, value in env.items():
            print(f"    {key} = {value}")

    try:
        pair = input(f"\n  {_BOLD}ENV>{_RESET} ").strip()
        if not pair:
            return
        if "=" in pair:
            key, value = pair.split("=", 1)
            env[key.strip()] = value.strip()
        else:
            env.pop(pair.strip(), None)
        tm.update_task_env(task["name"], env)
    except KeyboardInterrupt:
        pass


def _do_export(tasks: list[dict[str, object]]) -> None:
    """Export tasks to CSV or JSON."""
    print(f"\n  {_BOLD}Export Tasks{_RESET}")
    print(f"  {_DIM}Exporting {len(tasks)} task(s){_RESET}\n")
    export_format = input("  Format? [csv/json] (default: csv): ").strip().lower()
    if export_format != "json":
        export_format = "csv"

    filename = f"pyruns_export_{export_timestamp()}.{export_format}"
    content = build_export_json(tasks) if export_format == "json" else build_export_csv(tasks)
    if not content:
        print(f"  {_DIM}No data to export.{_RESET}")
        return

    with open(filename, "w", encoding="utf-8") as handle:
        handle.write(content)
    print(f"\n  Exported to {_BOLD}{os.path.abspath(filename)}{_RESET}")
