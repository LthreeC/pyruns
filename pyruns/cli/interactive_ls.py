"""
Interactive terminal UI for ``ls -i`` command.

Provides a scrollable, multi-select task list with quick-action hotkeys.
Uses VT100 alternate screen buffer for clean enter/exit.
"""
import os
import sys
import time as _time

from pyruns.utils.sort_utils import filter_tasks
from pyruns.utils.info_io import get_log_options, resolve_log_path
from pyruns.cli.display import (
    _get_terminal_width, _truncate, _status_str,
    _BOLD, _RESET, _DIM,
)


# ── Cross-platform single-key reader ──────────────────────────
if os.name == "nt":
    import msvcrt

    def _flush_input():
        while msvcrt.kbhit():
            msvcrt.getch()

    def getch() -> str:
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            ch2 = msvcrt.getch()
            if ch2 == b"\x48": return "up"
            if ch2 == b"\x50": return "down"
            return ""
        try:
            return ch.decode("utf-8", errors="ignore")
        except Exception:
            return ""
else:
    import tty, termios, select

    def _flush_input():
        fd = sys.stdin.fileno()
        while select.select([fd], [], [], 0)[0]:
            os.read(fd, 1024)

    def getch() -> str:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    if ch3 == "A": return "up"
                    if ch3 == "B": return "down"
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── Screen helpers ─────────────────────────────────────────────

def _enter_alt():
    sys.stdout.write("\033[?1049h")
    sys.stdout.flush()
    _time.sleep(0.05)
    _flush_input()

def _leave_alt():
    sys.stdout.write("\033[?1049l")
    sys.stdout.flush()


# ── Main Interactive Loop ──────────────────────────────────────

def run_interactive_ls(tm, query=""):
    from pyruns.cli.commands import _sorted_tasks, cmd_open

    cursor = 0
    selected: set = set()
    in_filter = False

    _enter_alt()

    try:
        while True:
            tm.refresh_from_disk(check_all=True)
            all_tasks = _sorted_tasks(tm)
            tasks = filter_tasks(all_tasks, query) if query else all_tasks

            if tasks and cursor >= len(tasks):
                cursor = len(tasks) - 1
            if not tasks:
                cursor = 0

            # ── Build frame ──
            out = ["\033[2J\033[H"]
            tw = _get_terminal_width()
            n_sel = len(selected)

            title = f"  {_BOLD}Pyruns Interactive View{_RESET}  [ {len(tasks)} tasks"
            if n_sel:
                title += f" | {n_sel} selected"
            title += " ]"
            if query:
                title += f"  {_DIM}(filter: '{query}'){_RESET}"
            out.append(f"\n{title}\n\n")

            idx_w = max(3, len(str(len(tasks))))
            status_w = 13
            time_w = 19
            name_w = max(12, tw - idx_w - 3 - status_w - time_w - 16)

            out.append(f"       {'#':<{idx_w}}  {'Status':<{status_w}} {'Name':<{name_w}}  {'Created':<{time_w}}\n")
            out.append(f"  {'─' * (tw - 4)}\n")

            if not tasks:
                out.append(f"  {_DIM}No tasks found.{_RESET}\n")
            else:
                win = 20
                start = max(0, cursor - win // 2)
                end = min(len(tasks), start + win)
                if end - start < win and len(tasks) > win:
                    start = max(0, end - win)

                for i in range(start, end):
                    t = tasks[i]
                    is_cur = (i == cursor)
                    tname = t.get("name") or ""
                    checked = tname in selected
                    chk = f"\033[33m■\033[0m" if checked else " "
                    name = _truncate(tname or "unnamed", name_w)
                    created = (t.get("created_at") or "")[:time_w]
                    runs = len(t.get("start_times") or [])
                    sc = _status_str(t.get("status") or "pending", runs)

                    ptr = f"{_BOLD}\033[36m>\033[0m" if is_cur else " "
                    if is_cur:
                        out.append(f"{ptr} [{chk}] {i+1:<{idx_w}}  {sc} {_BOLD}{name:<{name_w}}{_RESET}  {_DIM}{created}{_RESET}\n")
                    else:
                        out.append(f"{ptr} [{chk}] {i+1:<{idx_w}}  {sc} {name:<{name_w}}  {_DIM}{created}{_RESET}\n")

            out.append(f"\n  {'─' * (tw - 4)}\n")

            if in_filter:
                out.append(f"  {_BOLD}Filter:{_RESET} type query (empty=clear)\n")
                sys.stdout.write("".join(out))
                sys.stdout.flush()
                try:
                    query = input(f"  {_BOLD}Search: {_RESET}").strip()
                except KeyboardInterrupt:
                    pass
                in_filter = False
                cursor = 0
                _flush_input()
                continue

            keys = (
                f"  {_BOLD}KEYS:{_RESET} "
                f"[c] Select  [a] All  "
                f"[r] Run  [b] Batch  "
                f"[d] Delete  "
                f"[o] Open  [l] Log  [e] Env  [x] Export  "
                f"[f] Filter  [q] Quit"
            )
            out.append(keys + "\n")
            sys.stdout.write("".join(out))
            sys.stdout.flush()

            key = getch()

            if key in ("up", "k"):
                cursor = max(0, cursor - 1)
            elif key in ("down", "j"):
                if tasks:
                    cursor = min(len(tasks) - 1, cursor + 1)
            elif key in ("q", "\x03"):
                break
            elif key in ("f", "/"):
                in_filter = True

            # Select
            elif key == "c" and tasks:
                tname = tasks[cursor].get("name", "")
                if tname in selected:
                    selected.discard(tname)
                else:
                    selected.add(tname)

            elif key == "a":
                all_names = {t.get("name", "") for t in tasks}
                if selected >= all_names:
                    selected.clear()
                else:
                    selected = all_names.copy()

            # Run single
            elif key == "r" and tasks:
                t = tasks[cursor]
                if t.get("status") not in ("running", "queued"):
                    tm.start_task_now(t["name"])

            # Batch run selected
            elif key == "b" and selected:
                _leave_alt()
                _batch_run(tm, tasks, selected)
                selected.clear()
                _enter_alt()

            # Delete
            elif key == "d" and tasks:
                targets = [t for t in tasks if t.get("name") in selected] if selected else [tasks[cursor]]
                _leave_alt()
                _delete_tasks(tm, targets)
                selected.clear()
                _enter_alt()

            # Open
            elif key == "o" and tasks:
                cmd_open(tm, [tasks[cursor]["name"]])

            # Log
            elif key == "l" and tasks:
                _leave_alt()
                _view_log(tasks[cursor])
                _enter_alt()

            # Env
            elif key == "e" and tasks:
                _leave_alt()
                _edit_env(tasks[cursor])
                _enter_alt()

            # Export
            elif key == "x":
                export_tasks = [t for t in tasks if t.get("name") in selected] if selected else tasks
                _leave_alt()
                _do_export(export_tasks)
                input(f"\n  {_DIM}Press Enter to return...{_RESET}")
                _enter_alt()

    finally:
        _leave_alt()
        print(f"  {_DIM}Exited interactive mode.{_RESET}\n")


# ── Sub-actions ────────────────────────────────────────────────

def _batch_run(tm, tasks, selected):
    """Prompt for batch settings and run selected tasks."""
    names = [t["name"] for t in tasks if t.get("name") in selected and t.get("status") not in ("running", "queued")]
    if not names:
        print(f"\n  {_DIM}No runnable tasks selected.{_RESET}")
        input(f"  {_DIM}Press Enter to return...{_RESET}")
        return

    print(f"\n  {_BOLD}Batch Run{_RESET}  ({len(names)} task(s))")
    for n in names:
        print(f"    • {n}")

    try:
        mw = input(f"\n  Max workers (default=1): ").strip()
        max_workers = int(mw) if mw else 1
        mode = input(f"  Mode [thread/process] (default=thread): ").strip().lower()
        if mode not in ("process",):
            mode = "thread"

        tm.start_batch_tasks(names, execution_mode=mode, max_workers=max_workers)
        print(f"\n  ✔ Submitted {len(names)} task(s) in {mode} mode (workers={max_workers})")
    except KeyboardInterrupt:
        print(f"\n  Cancelled.")
    input(f"  {_DIM}Press Enter to return...{_RESET}")


def _delete_tasks(tm, targets):
    """Delete tasks with confirmation."""
    names = [t["name"] for t in targets]
    print(f"\n  {_BOLD}Delete Tasks{_RESET}")
    for n in names:
        print(f"    • {n}")

    try:
        answer = input(f"\n  Delete {len(names)} task(s)? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            tm.delete_tasks(names)
            print(f"  ✔ Deleted {len(names)} task(s)")
        else:
            print(f"  Cancelled.")
    except KeyboardInterrupt:
        print(f"\n  Cancelled.")
    input(f"  {_DIM}Press Enter to return...{_RESET}")


def _view_log(task):
    """View log files in alt-screen with n/p navigation.

    Uses log_emitter for real-time streaming (same mechanism as the UI),
    with file read only for historical content on initial draw.
    """
    import queue as _queue
    import shutil
    import threading
    from pyruns.utils.events import log_emitter

    task_dir = task.get("dir")
    task_name = task.get("name", "unknown")
    opts = get_log_options(task_dir)

    if not opts:
        print(f"\n  {_DIM}No log files for '{task_name}'{_RESET}")
        input(f"  {_DIM}Press Enter to return...{_RESET}")
        return

    keys = list(opts.keys())
    # Default: latest log (by mtime)
    default_path = resolve_log_path(task_dir)
    current_idx = 0
    if default_path:
        for i, k in enumerate(keys):
            if opts[k] == default_path:
                current_idx = i
                break

    # ── State for real-time streaming ──
    log_queue: _queue.Queue = _queue.Queue()

    def _on_live_chunk(chunk: str):
        log_queue.put(chunk)

    # Enter alt-screen
    sys.stdout.write("\033[?1049h")
    sys.stdout.flush()

    needs_redraw = True

    try:
        # Subscribe to log_emitter for live updates
        log_emitter.subscribe(task_name, _on_live_chunk)

        while True:
            log_name = keys[current_idx]
            log_path = opts[log_name]
            tw = _get_terminal_width()
            sep = f"  {'─' * (tw - 4)}"
            nav = f"  {_BOLD}KEYS:{_RESET} [n] Next log  [p] Prev log  [q] Back"

            if needs_redraw:
                opts = get_log_options(task_dir)
                if not opts:
                    opts = {log_name: log_path}
                keys = list(opts.keys())
                # Ensure current_idx is within bounds
                current_idx = min(current_idx, len(keys) - 1)
                log_name = keys[current_idx]
                log_path = opts[log_name]

                out = ["\033[2J\033[H"]
                out.append(f"\n  {_BOLD}── {task_name} ──{_RESET}  {log_name}  ({current_idx + 1}/{len(keys)})\n")
                out.append(sep + "\n")

                try:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    if content:
                        lines = content.splitlines()
                        term_h = shutil.get_terminal_size().lines - 8
                        if len(lines) > term_h:
                            out.append(f"  {_DIM}... ({len(lines) - term_h} lines omitted) ...{_RESET}\n\n")
                            lines = lines[-term_h:]
                        for line in lines:
                            out.append(f"  {line}\n")
                    else:
                        out.append(f"  {_DIM}(empty - waiting for output...){_RESET}\n")
                except Exception as e:
                    out.append(f"  Error: {e}\n")

                out.append(f"\n{sep}\n")
                out.append(nav + "\n")
                sys.stdout.write("".join(out))
                sys.stdout.flush()
                needs_redraw = False

                # Clear any queued chunks that are already in the file content
                while not log_queue.empty():
                    try:
                        log_queue.get_nowait()
                    except _queue.Empty:
                        break

            # ── Live tail: drain real-time chunks from log_emitter ──
            try:
                while True:
                    chunk = log_queue.get_nowait()
                    # Move cursor up to overwrite keys/sep, print chunk, reprint keys/sep
                    sys.stdout.write("\033[3A")  # Move up 3 lines (nav, empty line, sep)
                    sys.stdout.write("\033[J")   # Clear from cursor to end
                    # Convert \r\n back to \n for raw terminal
                    for line in chunk.replace('\r\n', '\n').splitlines():
                        sys.stdout.write(f"  {line}\n")
                    sys.stdout.write(f"\n{sep}\n")
                    sys.stdout.write(nav + "\n")
                    sys.stdout.flush()
            except _queue.Empty:
                pass

            # ── Non-blocking key check (cross-platform) ──
            if os.name == "nt":
                import msvcrt
                if msvcrt.kbhit():
                    key = getch()
                    if key in ("q", "\x03"):
                        break
                    elif key == "n":
                        current_idx = (current_idx + 1) % len(keys)
                        needs_redraw = True
                    elif key == "p":
                        current_idx = (current_idx - 1) % len(keys)
                        needs_redraw = True
                else:
                    _time.sleep(0.05)
            else:
                import select
                if select.select([sys.stdin], [], [], 0.05)[0]:
                    key = getch()
                    if key in ("q", "\x03"):
                        break
                    elif key == "n":
                        current_idx = (current_idx + 1) % len(keys)
                        needs_redraw = True
                    elif key == "p":
                        current_idx = (current_idx - 1) % len(keys)
                        needs_redraw = True

    finally:
        log_emitter.unsubscribe(task_name, _on_live_chunk)
        sys.stdout.write("\033[?1049l")
        sys.stdout.flush()


def _edit_env(t):
    """Edit custom_env for a task via task_info.json."""
    from pyruns.utils.info_io import load_task_info, save_task_info

    print(f"\n  {_BOLD}Environment Variables for '{t['name']}'{_RESET}")
    print(f"  {_DIM}Type KEY=VALUE to set, or KEY to delete. Blank to cancel.{_RESET}\n")

    info = load_task_info(t["dir"])
    env = info.get("custom_env", {}) or {}

    if not env:
        print(f"  {_DIM}(No custom variables){_RESET}")
    else:
        for k, v in env.items():
            print(f"    {k} = {v}")

    try:
        pair = input(f"\n  {_BOLD}ENV>{_RESET} ").strip()
        if pair:
            if "=" in pair:
                k, v = pair.split("=", 1)
                env[k.strip()] = v.strip()
            else:
                env.pop(pair.strip(), None)
            info["custom_env"] = env
            save_task_info(t["dir"], info)
    except KeyboardInterrupt:
        pass


def _do_export(tasks):
    """Export tasks to CSV or JSON file."""
    from pyruns.core.report import build_export_csv, build_export_json, export_timestamp

    print(f"\n  {_BOLD}Export Tasks{_RESET}")
    print(f"  {_DIM}Exporting {len(tasks)} task(s){_RESET}\n")
    fmt = input(f"  Format? [csv/json] (default: csv): ").strip().lower()
    if fmt not in ("json",):
        fmt = "csv"

    ts = export_timestamp()
    filename = f"pyruns_export_{ts}.{fmt}"

    if fmt == "json":
        content = build_export_json(tasks)
    else:
        content = build_export_csv(tasks)

    if not content:
        print(f"  {_DIM}No data to export.{_RESET}")
        return

    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"\n  ✔ Exported to {_BOLD}{os.path.abspath(filename)}{_RESET}")
