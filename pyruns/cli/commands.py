"""
CLI commands — ls, gen, run, delete, jobs, fg.

Each command is a plain function that takes a ``TaskManager`` (and optional args)
and prints results to stdout.  All task lifecycle logic is delegated to
``core.task_manager`` and ``core.task_generator`` so CLI and UI share the same
business logic.
"""
import os
import shutil

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
    valid = [t for t in tm.tasks if t is not None]
    return sorted(valid, key=task_sort_key, reverse=True)


def _get_git_editor() -> str:
    """
    100% 像素级复刻 git/editor.c 中的 git_editor() 逻辑
    """
    # 1. 对应 getenv("GIT_EDITOR")
    editor = os.environ.get("GIT_EDITOR")
    if editor: return editor

    # 2. 对应 editor_program (即 git config core.editor)
    try:
        editor = subprocess.check_output(
            ["git", "config", "--get", "core.editor"], 
            stderr=subprocess.DEVNULL, text=True
        ).strip()
        if editor: return editor
    except Exception:
        pass

    # 3. 对应 getenv("VISUAL")
    editor = os.environ.get("VISUAL")
    if editor: return editor

    # 4. 对应 getenv("EDITOR")
    editor = os.environ.get("EDITOR")
    if editor: return editor

    # 5. 额外彩蛋：如果用户连 Git 都没配，但确实在 VSCode/Cursor 终端里，帮他一把
    term = os.environ.get("TERM_PROGRAM", "")
    if term == "vscode":
        ipc = os.environ.get("VSCODE_IPC_HOOK_CLI", "").lower()
        if "cursor" in ipc: return "cursor --wait"
        return "code --wait"

    # 6. 对应 DEFAULT_EDITOR
    return "notepad" if os.name == "nt" else "vi"

def _resolve_targets(tm, args: List[str]) -> list:
    """Resolve user-provided names or 1-based indices to task dicts."""
    sorted_tasks = _sorted_tasks(tm)
    targets = []
    for arg in args:
        try:
            idx = int(arg)
            if 1 <= idx <= len(sorted_tasks):
                targets.append(sorted_tasks[idx - 1])
                continue
        except ValueError:
            pass
        for t in sorted_tasks:
            if t.get("name") == arg:
                targets.append(t)
                break
        else:
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
    args = args or []
    if "-i" in args:
        args_without_i = [a for a in args if a != "-i"]
        from pyruns.cli.interactive_ls import run_interactive_ls
        run_interactive_ls(tm, query=" ".join(args_without_i))
        return

    query = " ".join(args)
    tasks = _sorted_tasks(tm)
    if query:
        tasks = filter_tasks(tasks, query)
    print_task_table(tasks)


def cmd_generate(tm, args: List[str] = None) -> None:
    """``gen [template_path]`` — open editor for YAML config, create tasks on save."""
    from pyruns.core.task_generator import TaskGenerator
    from pyruns.utils.batch_utils import generate_batch_configs

    workspace_dir = os.path.dirname(tm.tasks_dir)

    template_path = None
    if args:
        candidate = args[0]
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

    with open(template_path, "r", encoding="utf-8") as f:
        template_content = f.read()

    header = (
        "# ── Pyruns Task Generator ──────────────────────────────────\n"
        "# Edit parameters below, then SAVE and CLOSE to generate tasks.\n"
        "# Use pipe syntax for batch: lr: 0.001 | 0.01 | 0.1\n"
        "# Lines starting with # are comments.\n"
        "# ──────────────────────────────────────────────────────────\n\n"
    )

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix="pyruns_gen_",
        delete=False, encoding="utf-8",
    ) as tmp:
        tmp.write(header + template_content)
        tmp_path = tmp.name

    # ---------------------------------------------------------
    # 核心执行逻辑：完全效仿 Git 的 launch_specified_editor
    # ---------------------------------------------------------
    editor = _get_git_editor()
    
    # 完美复刻 Git 的提示语："hint: Waiting for your editor to close the file..."
    print(f"  hint: Waiting for your editor '{editor}' to close the file...")
    
    try:
        # p.use_shell = 1 的 Python 实现：直接拼装字符串并开启 shell=True！
        # 这样 Windows 的 cmd.exe 会自动处理 Cursor / VSCode 的调用，不再报 FileNotFoundError！
        cmd = f'{editor} "{tmp_path}"'
        subprocess.run(cmd, shell=True, check=True)
        
    except Exception as e:
        print(f"  error: there was a problem with the editor '{editor}'")
        os.unlink(tmp_path)
        return

    # 代码走到这里，说明用户已经在 IDE 里保存，并关闭了页签
    config = load_yaml(tmp_path)
    try:
        os.unlink(tmp_path)
    except OSError:
        pass

    if not config:
        print("  Empty config — no tasks generated.")
        return

    configs = generate_batch_configs(config)
    n = len(configs)

    for i, cfg in enumerate(configs):
        preview = preview_config_line(cfg, max_items=5)
        tag = f"[{i+1}/{n}]" if n > 1 else ""
        print(f"  {tag} {preview}")

    answer = input(f"\n  Generate {n} task(s)? [Y/n] ").strip().lower()
    if answer and answer not in ("y", "yes"):
        print("  Cancelled.")
        return

    name_prefix = input("  Task name prefix (blank=auto): ").strip()

    gen = TaskGenerator(root_dir=tm.tasks_dir)
    new_tasks = gen.create_tasks(configs, name_prefix)
    for t in new_tasks:
        tm.add_task(t)

    print(f"\n  ✔ Created {len(new_tasks)} task(s)")
    for t in new_tasks:
        print(f"    • {t['name']}")
    print()


def cmd_run(tm, args: List[str] = None) -> None:
    """``run <name|index> [...]`` — run tasks. Multiple args triggers batch mode.

    Single task: submits and auto-attaches to the log stream (like UI).
    Multiple tasks: prompts for batch settings, submits and returns.
    """
    if not args:
        print("  Usage: run <name|index> [name|index ...]")
        return

    targets = _resolve_targets(tm, args)
    if not targets:
        return

    if len(targets) == 1:
        t = targets[0]
        if t.get("status") == "running":
            print(f"  '{t['name']}' is already running — skipping.")
            return
        print(f"  Starting '{t['name']}'...")
        tm.start_task_now(t["name"])
        print(f"  ✔ Submitted 1 task\n")
        # Auto-attach: stream log output in real-time (like fg)
        time.sleep(0.3)  # brief pause for log file to be created
        cmd_fg(tm, [t["name"]])
    else:
        # Batch mode
        names = [t["name"] for t in targets if t.get("status") not in ("running", "queued")]
        if not names:
            print("  No runnable tasks found.")
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
            print("\n  Cancelled.")
    print()


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
    """``fg <name|index>`` — tail a task's log file (Ctrl+C to exit).

    Uses log_emitter for real-time streaming (same mechanism as the UI),
    falling back to file read only for the initial historical content.
    """
    import queue as _queue
    from pyruns.utils.events import log_emitter

    if not args:
        print("  Usage: fg <name|index>")
        return

    targets = _resolve_targets(tm, [args[0]])
    if not targets:
        return
    target = targets[0]

    task_dir = target.get("dir")
    task_name = target.get("name", "unknown")

    log_path = resolve_log_path(task_dir)
    if not log_path or not os.path.exists(log_path):
        print(f"  No log file found for '{task_name}'")
        return

    # ── Print header ──
    print(f"\n  {_BOLD}── {task_name} ──{_RESET}  (Ctrl+C to exit)\n")

    # ── Phase 1: dump existing file content (historical) ──
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
            if content:
                sys.stdout.write(content)
                sys.stdout.flush()
    except Exception:
        pass

    # ── Phase 2: subscribe to log_emitter for real-time chunks ──
    log_queue: _queue.Queue = _queue.Queue()
    _DONE = object()  # sentinel

    def _on_chunk(chunk: str):
        log_queue.put(chunk)

    log_emitter.subscribe(task_name, _on_chunk)

    try:
        while True:
            # Drain all available chunks without blocking
            try:
                while True:
                    chunk = log_queue.get(timeout=0.2)
                    if chunk is _DONE:
                        raise StopIteration
                    # log_emitter sends \r\n for xterm; convert back for terminal
                    sys.stdout.write(chunk.replace('\r\n', '\n'))
                    sys.stdout.flush()
            except _queue.Empty:
                pass
            except StopIteration:
                break

            # Check if the task is still running
            tm.refresh_from_disk(task_ids=[task_name])
            found = None
            for t in tm.tasks:
                if t and t.get("name") == task_name:
                    found = t
                    break
            if found and found.get("status") not in ("running", "queued"):
                # Drain any remaining chunks
                while not log_queue.empty():
                    chunk = log_queue.get_nowait()
                    sys.stdout.write(chunk.replace('\r\n', '\n'))
                    sys.stdout.flush()
                status = found.get("status", "unknown")
                style = _STATUS_STYLES.get(status, "")
                print(f"\n\n  {_colored(f'Task {status}', style)}\n")
                break
    except KeyboardInterrupt:
        print(f"\n\n  {_DIM}Detached from {task_name}{_RESET}\n")
    finally:
        log_emitter.unsubscribe(task_name, _on_chunk)



def cmd_fg(tm, args: List[str] = None) -> None:
    """``fg <name|index>`` — tail a task's log file inline (Ctrl+C to exit)."""
    import queue as _queue
    import time
    from pyruns.utils.events import log_emitter
    from pyruns.utils.info_io import load_task_info

    if not args:
        print("  Usage: fg <name|index>")
        return

    targets = _resolve_targets(tm, [args[0]])
    if not targets:
        return
    task_dir = targets[0].get("dir")
    task_name = targets[0].get("name", "unknown")

    # Determine which log file strictly represents the current operation
    tm.refresh_from_disk(task_ids=[task_name])
    t_info = load_task_info(task_dir) or {}
    run_idx = t_info.get("run_index", 1) if t_info.get("status") in ("running", "queued") else max(1, len(t_info.get("start_times", [])))
    
    from pyruns._config import RUN_LOGS_DIR
    target_log = os.path.join(task_dir, RUN_LOGS_DIR, f"run{run_idx}.log")

    print(f"\n  {_BOLD}── {task_name} ──{_RESET}  (Ctrl+C to exit)\n")

    # Wait up to 1s for the file to be created if it's currently queued
    for _ in range(10):
        if os.path.exists(target_log):
            break
        time.sleep(0.1)

    # Dump existing historical chunk of THIS specific run ONLY
    try:
        with open(target_log, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
            if content:
                sys.stdout.write(content.replace('\r\n', '\n'))
                sys.stdout.flush()
    except Exception:
        pass

    log_queue: _queue.Queue = _queue.Queue()
    _DONE = object()

    def _on_chunk(chunk: str):
        log_queue.put(chunk)

    log_emitter.subscribe(task_name, _on_chunk)

    try:
        while True:
            try:
                while True:
                    chunk = log_queue.get(timeout=0.2)
                    if chunk is _DONE:
                        raise StopIteration
                    sys.stdout.write(chunk.replace('\r\n', '\n'))
                    sys.stdout.flush()
            except _queue.Empty:
                pass
            except StopIteration:
                break

            # Poll status to see if task finished
            found = next((t for t in tm.tasks if t and t.get("name") == task_name), None)
            if found and found.get("status") not in ("running", "queued"):
                while not log_queue.empty():
                    sys.stdout.write(log_queue.get_nowait().replace('\r\n', '\n'))
                    sys.stdout.flush()
                status_str = found.get("status", "unknown")
                style = _STATUS_STYLES.get(status_str, "")
                print(f"\n\n  {_colored(f'Task {status_str}', style)}\n")
                break
    except KeyboardInterrupt:
        print(f"\n\n  {_DIM}Detached from {task_name}{_RESET}\n")
    finally:
        log_emitter.unsubscribe(task_name, _on_chunk)


def cmd_log(tm, args: List[str] = None) -> None:
    """``log <name|index>`` — view log files in alt-screen viewer."""
    if not args:
        print("  Usage: log <name|index>")
        return

    targets = _resolve_targets(tm, [args[0]])
    if not targets:
        return
    target = targets[0]

    from pyruns.cli.interactive_ls import _view_log
    _view_log(target)


def cmd_open(tm, args: List[str] = None) -> None:
    """``open <name|index> [config|task]`` — open a task's config.yaml or task_info.json in the editor."""
    if not args:
        print("  Usage: open <name|index> [config|task]")
        return
        
    targets = _resolve_targets(tm, [args[0]])
    if not targets:
        return
        
    # Open the first matched target
    target = targets[0]
    task_dir = target.get("dir")
    from pyruns._config import CONFIG_FILENAME, TASK_INFO_FILENAME
    
    file_type = args[1].lower() if len(args) > 1 else "config"
    if file_type == "task":
        config_path = os.path.join(task_dir, TASK_INFO_FILENAME)
    else:
        config_path = os.path.join(task_dir, CONFIG_FILENAME)
    
    if not os.path.exists(config_path):
        print(f"  File not found: {config_path}")
        return
        
    # Non-blocking: open in editor and return immediately
    editor = _get_git_editor()
    # Strip --wait flag for open (we don't want to block the REPL)
    editor_no_wait = editor.replace(" --wait", "").replace(" -w", "")
    print(f"  Opening {os.path.basename(config_path)} ...")
    try:
        cmd = f'{editor_no_wait} "{config_path}"'
        subprocess.Popen(cmd, shell=True)
    except Exception as e:
        print(f"  Failed to open editor: {e}")


def cmd_stat(tm, args: List[str] = None) -> None:
    """``stat`` / ``status`` — show system metrics. Use ``stat -i`` for live refresh."""
    args = args or []
    interactive = "-i" in args
    
    if interactive:
        _stat_interactive()
    else:
        _stat_once()


def cmd_info(tm, args: List[str] = None) -> None:
    """``info`` — show current workspace info."""
    import json
    from pyruns.cli.display import _get_terminal_width

    print(f"\n  {_BOLD}Workspace Info{_RESET}")
    tw = _get_terminal_width()
    sep = f"  {'─' * (tw - 4)}"
    print(sep)

    tasks_dir = getattr(tm, 'tasks_dir', None)
    ws_dir = os.path.dirname(tasks_dir) if tasks_dir else None

    if ws_dir:
        print(f"  Workspace:  {ws_dir}")
        script_info_path = os.path.join(ws_dir, "script_info.json")
        if os.path.exists(script_info_path):
            try:
                with open(script_info_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
                print(f"  Script:     {info.get('script_name', 'N/A')}")
                sp = info.get('script_path', '')
                if sp:
                    print(f"  Path:       {sp}")
            except Exception:
                pass
    else:
        print(f"  {_DIM}No workspace detected.{_RESET}")

    print(f"  Tasks:      {len(tm.tasks)} total")
    active = sum(1 for t in tm.tasks if t and t.get('status') in ('running', 'queued'))
    if active:
        print(f"  Active:     {active} running/queued")
    print(sep)
    print()


def _stat_once() -> None:
    """Print system metrics once."""
    from pyruns.core.system_metrics import SystemMonitor
    from pyruns.cli.display import _get_terminal_width
    
    monitor = SystemMonitor()
    metrics = monitor.sample()
    
    cpu = metrics.get("cpu_percent", 0)
    mem = metrics.get("mem_percent", 0)
    mem_info = __import__('psutil').virtual_memory()
    gpus = metrics.get("gpus", [])
    
    tw = _get_terminal_width()
    sep = f"  {'─' * (tw - 4)}"
    
    print(f"\n  {_BOLD}System Metrics{_RESET}")
    print(sep)
    
    print(f"  CPU:  {_bar(cpu)}")
    print(f"  RAM:  {_bar(mem)}  ({mem_info.used // (1024**2):,} / {mem_info.total // (1024**2):,} MB)")
    
    if gpus:
        print()
        for g in gpus:
            util = g.get("util", 0)
            mu = g.get("mem_used", 0)
            mt = max(g.get("mem_total", 1), 1)
            mp = (mu / mt) * 100
            idx = g.get("index", "?")
            print(f"  GPU {idx}:  {_bar(util)}  VRAM {_bar(mp)}  ({int(mu)} / {int(mt)} MB)")
    else:
        print(f"\n  {_DIM}No GPUs detected.{_RESET}")
        
    print(sep)
    print()


def _stat_interactive() -> None:
    """Auto-refresh stat view (like gpustat -i)."""
    from pyruns.core.system_metrics import SystemMonitor
    from pyruns.cli.display import _get_terminal_width
    import psutil as _psutil
    
    monitor = SystemMonitor()
    
    # Enter alt screen
    sys.stdout.write("\033[?1049h")
    sys.stdout.flush()
    
    try:
        while True:
            metrics = monitor.sample()
            cpu = metrics.get("cpu_percent", 0)
            mem = metrics.get("mem_percent", 0)
            mem_info = _psutil.virtual_memory()
            gpus = metrics.get("gpus", [])
            tw = _get_terminal_width()
            sep = f"  {'─' * (tw - 4)}"
            
            out = ["\033[2J\033[H"]
            out.append(f"\n  {_BOLD}System Metrics{_RESET}  {_DIM}(refreshing every 1s, press Ctrl+C to exit){_RESET}\n")
            out.append(sep + "\n")
            out.append(f"  CPU:  {_bar(cpu)}\n")
            out.append(f"  RAM:  {_bar(mem)}  ({mem_info.used // (1024**2):,} / {mem_info.total // (1024**2):,} MB)\n")
            
            if gpus:
                out.append("\n")
                for g in gpus:
                    util = g.get("util", 0)
                    mu = g.get("mem_used", 0)
                    mt = max(g.get("mem_total", 1), 1)
                    mp = (mu / mt) * 100
                    idx = g.get("index", "?")
                    out.append(f"  GPU {idx}:  {_bar(util)}  VRAM {_bar(mp)}  ({int(mu)} / {int(mt)} MB)\n")
            else:
                out.append(f"\n  {_DIM}No GPUs detected.{_RESET}\n")
            
            out.append(sep + "\n")
            sys.stdout.write("".join(out))
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
    p = max(0, min(100, percent))
    filled = int(width * p / 100)
    empty = width - filled
    if p < 60:
        color = "\033[32m"  # Green
    elif p < 85:
        color = "\033[33m"  # Yellow
    else:
        color = "\033[31m"  # Red
    return f"{color}{'█' * filled}{_DIM}{'░' * empty}{_RESET}  {color}{percent:5.1f}%{_RESET}"





# ═══════════════════════════════════════════════════════════════
#  Command registry
# ═══════════════════════════════════════════════════════════════

COMMANDS = {
    "ls":       cmd_list,
    "list":     cmd_list,

    "gen":      cmd_generate,
    "generate": cmd_generate,
    "gentask":  cmd_generate,

    "run":      cmd_run,
    "delete":   cmd_delete,
    "del":      cmd_delete,
    "rm":       cmd_delete,
    
    "open":     cmd_open,
    
    "log":      cmd_log,

    "fg":       cmd_fg,
    
    
    "jobs":     cmd_jobs,
    "stat":     cmd_stat,
    "status":   cmd_stat,
    
    "info":     cmd_info,
}
