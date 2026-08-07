"""Implementation of the one-shot Pyruns command line interface."""

from __future__ import annotations

import copy
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psutil
import yaml

from pyruns._config import (
    DEFAULT_ROOT_NAME,
    ENV_KEY_CLI_TERMINAL_RUNTIME,
    ENV_KEY_ROOT,
    QUEUE_LOG_FILENAME,
    RUN_LOGS_DIR,
    SCRIPT_INFO_FILENAME,
    SHELL_WORKSPACE_NAME,
    TASKS_DIR,
    TRASH_DIR,
    WORKSPACE_KIND_SCRIPT,
    WORKSPACE_KIND_SHELL,
)
from pyruns.cli.runner import submit_cli_tasks
from pyruns.core.report import build_export_csv, build_export_json
from pyruns.core.system_metrics import SystemMonitor
from pyruns.core.task_generator import TaskGenerator
from pyruns.core.task_manager import TaskManager
from pyruns.launcher import (
    bootstrap_shell_workspace,
    bootstrap_workspace,
    launcher_query,
    mark_workspace_active,
    resolve_workspace_for_script,
)
from pyruns.utils.batch_utils import generate_batch_configs
from pyruns.utils.config_utils import load_yaml_strict, safe_filename
from pyruns.utils.env_utils import is_valid_environment_name, normalize_environment
from pyruns.utils.info_io import (
    load_script_info,
    load_task_info,
    resolve_log_path,
    run_slot_count,
    update_task_info,
    validate_existing_task_name,
    validate_task_name,
)
from pyruns.utils.log_io import safe_read_log
from pyruns.utils.parse_utils import resolve_config_path
from pyruns.utils.settings import (
    SETTINGS_DEFAULTS,
    _settings_path,
    ensure_settings_file,
    load_settings,
    reload_settings,
    save_setting_for_root,
    setting_numbers_are_finite,
)
from pyruns.utils.shell_runtime import (
    build_script_file_argv,
    get_shell_runtime_for_workspace,
)
from pyruns.utils.task_files import resolve_task_config_file


_ACTIVE_STATUSES = {"queued", "running"}
_FINAL_STATUSES = {"completed", "failed", "cancelled"}
_VALID_STATUSES = {"pending", "queued", "running", "completed", "failed", "cancelled"}
_CMD_META_CHARS = frozenset("&|<>^()%!")
_SHELL_SCRIPT_EXTENSIONS = frozenset({".sh", ".ps1", ".cmd", ".bat"})


class CliError(RuntimeError):
    """User-facing command failure."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class CliUsageError(CliError):
    """Invalid command-line combination not expressible through argparse."""

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=2)


def _eprint(message: str = "") -> None:
    print(message, file=sys.stderr)


def _program(context: Any) -> str:
    return str(getattr(context, "program", "pyr") or "pyr")


def _json_dump(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False))


def _normalized_path(path: str) -> str:
    return os.path.abspath(path).replace("\\", "/")


def _find_project_root(start: str | None = None) -> str | None:
    """Find the nearest project-level ``_pyruns_`` directory."""

    current = Path(start or os.getcwd()).resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        if directory.name == DEFAULT_ROOT_NAME and directory.is_dir():
            return _normalized_path(str(directory))
        candidate = directory / DEFAULT_ROOT_NAME
        if candidate.is_dir():
            return _normalized_path(str(candidate))
    return None


def _workspace_directories(project_root: str) -> list[str]:
    root = Path(project_root)
    if not root.is_dir():
        return []
    return [
        _normalized_path(str(path))
        for path in sorted(root.iterdir(), key=lambda item: item.name.lower())
        if path.is_dir() and (path / SCRIPT_INFO_FILENAME).is_file()
    ]


def _workspace_label(workspace: str) -> str:
    info = load_script_info(workspace)
    kind = str(info.get("workspace_kind", "") or "")
    if kind == WORKSPACE_KIND_SHELL or os.path.basename(workspace) == SHELL_WORKSPACE_NAME:
        return "shell"
    return os.path.basename(workspace)


def _resolve_workspace_selector(selector: str, project_root: str | None) -> str:
    raw = str(selector or "").strip()
    if not raw:
        raise CliUsageError("workspace selector cannot be empty")

    if raw.lower() == "shell":
        if not project_root:
            raise CliError("no Pyruns project found; run 'pyr init' first")
        candidate = os.path.join(project_root, SHELL_WORKSPACE_NAME)
        if not os.path.isfile(os.path.join(candidate, SCRIPT_INFO_FILENAME)):
            raise CliError("shell workspace does not exist; run 'pyr init' first")
        return _normalized_path(candidate)

    path_candidate = os.path.abspath(os.path.expanduser(os.path.expandvars(raw)))
    if os.path.isfile(path_candidate) and path_candidate.lower().endswith(".py"):
        workspace = resolve_workspace_for_script(path_candidate)
        if not workspace:
            raise CliError(f"script workspace does not exist for '{raw}'; run 'pyr init {raw}' first")
        return _normalized_path(workspace)
    if os.path.isdir(path_candidate) and os.path.isfile(
        os.path.join(path_candidate, SCRIPT_INFO_FILENAME)
    ):
        return _normalized_path(path_candidate)

    if project_root:
        direct = os.path.join(project_root, raw)
        if os.path.isfile(os.path.join(direct, SCRIPT_INFO_FILENAME)):
            return _normalized_path(direct)

        matches: list[str] = []
        for workspace in _workspace_directories(project_root):
            info = load_script_info(workspace)
            if str(info.get("script_name", "") or "") == raw:
                matches.append(workspace)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise CliError(
                f"workspace selector '{raw}' is ambiguous: "
                + ", ".join(os.path.basename(item) for item in matches)
            )

    raise CliError(f"workspace not found: {raw}")


def resolve_workspace(context: Any) -> str:
    """Resolve exactly one workspace without using the active-workspace marker."""

    project_root = _find_project_root()
    selector = str(context.workspace or "").strip()
    if selector:
        return _resolve_workspace_selector(selector, project_root)
    if not project_root:
        raise CliError("no Pyruns project found; run 'pyr init' first")

    workspaces = _workspace_directories(project_root)
    if not workspaces:
        raise CliError("no Pyruns workspace found; run 'pyr init' first")
    if len(workspaces) > 1:
        labels = ", ".join(_workspace_label(item) for item in workspaces)
        raise CliError(f"multiple workspaces found ({labels}); select one with -w/--workspace")
    return workspaces[0]


def _shell_workspace_for_exec(context: Any, *, create: bool = True) -> str:
    project_root = _find_project_root()
    if context.workspace:
        selector = str(context.workspace).strip()
        if selector.lower() == "shell":
            if not project_root:
                project_root = _normalized_path(os.path.join(os.getcwd(), DEFAULT_ROOT_NAME))
            shell_workspace = _normalized_path(os.path.join(project_root, SHELL_WORKSPACE_NAME))
            return (
                _normalized_path(bootstrap_shell_workspace(project_root))
                if create
                else shell_workspace
            )

        selected = _resolve_workspace_selector(selector, project_root)
        info = load_script_info(selected)
        if (
            str(info.get("workspace_kind", "") or "") != WORKSPACE_KIND_SHELL
            and os.path.basename(selected) != SHELL_WORKSPACE_NAME
        ):
            raise CliUsageError("exec requires the shell workspace; use '-w shell' or omit -w")
        return selected

    if not project_root:
        project_root = _normalized_path(os.path.join(os.getcwd(), DEFAULT_ROOT_NAME))
    shell_workspace = _normalized_path(os.path.join(project_root, SHELL_WORKSPACE_NAME))
    return (
        _normalized_path(bootstrap_shell_workspace(project_root))
        if create
        else shell_workspace
    )


def _workspace_kind(workspace: str) -> str:
    info = load_script_info(workspace)
    kind = str(info.get("workspace_kind", "") or "")
    if kind:
        return kind
    return WORKSPACE_KIND_SHELL if os.path.basename(workspace) == SHELL_WORKSPACE_NAME else WORKSPACE_KIND_SCRIPT


@contextmanager
def _task_manager(workspace: str, *, lazy_scan: bool | None = False) -> Iterator[TaskManager]:
    tasks_dir = os.path.join(workspace, TASKS_DIR)
    os.makedirs(tasks_dir, exist_ok=True)
    os.environ[ENV_KEY_ROOT] = workspace
    os.environ[ENV_KEY_CLI_TERMINAL_RUNTIME] = "1"
    ensure_settings_file(workspace)
    load_settings(workspace)
    manager = TaskManager(
        tasks_dir=tasks_dir,
        lazy_scan=lazy_scan,
        owns_task_lifecycle=False,
    )
    try:
        yield manager
    finally:
        manager.shutdown()


def _refresh_tasks(manager: TaskManager) -> list[dict[str, Any]]:
    if not getattr(manager, "_disk_scan_complete", True):
        manager.scan_disk()
    else:
        manager.refresh_from_disk(check_all=True)
    return [task for task in manager.tasks if task is not None]


def _resolve_exact_tasks(manager: TaskManager, names: list[str]) -> list[dict[str, Any]]:
    """Resolve every target by exact name and fail atomically if any is absent."""

    tasks: list[dict[str, Any]] = []
    missing: list[str] = []
    seen: set[str] = set()
    for raw_name in names:
        name = str(raw_name or "")
        if name in seen:
            continue
        seen.add(name)
        task = manager.load_task_by_name(name)
        if task is None or str(task.get("name", "") or "") != name:
            missing.append(name)
        else:
            tasks.append(task)
    if missing:
        raise CliError("task not found: " + ", ".join(missing))
    return tasks


def _parse_task_run_reference(value: str) -> tuple[str, int | None]:
    """Parse an exact task name, optionally followed by ``@RUN``."""

    reference = str(value or "")
    if "@" not in reference:
        return reference, None
    task_name, run_text = reference.rsplit("@", 1)
    if not run_text.isdecimal():
        return reference, None
    if not task_name or int(run_text) <= 0:
        raise CliUsageError(
            f"invalid task run reference '{reference}'; RUN must be a positive integer"
        )
    return task_name, int(run_text)


def _resolve_task_run_reference(
    manager: TaskManager,
    value: str,
) -> tuple[dict[str, Any], int | None]:
    """Resolve an exact task name before interpreting a legacy-compatible ``@RUN`` suffix."""

    reference = str(value or "")
    exact = manager.load_task_by_name(reference)
    if exact is not None and str(exact.get("name", "") or "") == reference:
        return exact, None

    task_name, selected_run = _parse_task_run_reference(reference)
    return _resolve_exact_tasks(manager, [task_name])[0], selected_run


def _latest_run_index(task: dict[str, Any], info: dict[str, Any] | None = None) -> int:
    source = info or task
    try:
        run_index = int(source.get("run_index", 0) or 0)
    except (TypeError, ValueError):
        run_index = 0
    if run_index > 0:
        return run_index
    return len(source.get("start_times", []) or [])


def _selected_run_record(task: dict[str, Any], run_index: int) -> dict[str, Any]:
    """Return one validated historical run summary."""

    task_dir = str(task.get("dir", "") or "")
    info = load_task_info(task_dir) or task
    total = run_slot_count(info)
    if run_index > total:
        name = str(task.get("name", "") or "")
        message = (
            f"run {run_index} not found for task '{name}'; available runs: 1-{total}"
            if total
            else f"run {run_index} not found for task '{name}'; task has no runs"
        )
        raise CliError(message)

    def value_at(key: str) -> Any:
        values = list(info.get(key, []) or [])
        return values[run_index - 1] if run_index <= len(values) else None

    log_path = _log_path(task, run_index=run_index)
    return {
        "index": run_index,
        "start_time": value_at("start_times") or None,
        "finish_time": value_at("finish_times") or None,
        "pid": value_at("pids") or None,
        "duration_seconds": value_at("durations"),
        "exit_code": value_at("exit_codes"),
        "source_state": value_at("source_states") or None,
        "record": value_at("records") or {},
        "track": value_at("tracks") or {},
        "log": _normalized_path(log_path) if os.path.isfile(log_path) else None,
    }


def _log_path(task: dict[str, Any], *, run_index: int | None = None) -> str:
    task_dir = str(task.get("dir", "") or "")
    info = load_task_info(task_dir) or task
    status = str(info.get("status", task.get("status", "")) or "").lower()
    if run_index is None and status == "queued":
        return os.path.join(task_dir, RUN_LOGS_DIR, QUEUE_LOG_FILENAME)
    selected_run = run_index or _latest_run_index(task, info)
    if selected_run <= 0:
        selected_run = 1
    run_path = os.path.join(task_dir, RUN_LOGS_DIR, f"run{selected_run}.log")
    if os.path.isfile(run_path):
        return run_path
    if selected_run == _latest_run_index(task, info):
        fallback = resolve_log_path(task_dir)
        if fallback:
            return fallback
    return run_path


def _task_record(
    task: dict[str, Any],
    *,
    detailed: bool = False,
    selected_run: int | None = None,
) -> dict[str, Any]:
    task_dir = str(task.get("dir", "") or "")
    info = load_task_info(task_dir) or {}
    status = str(info.get("status", task.get("status", "pending")) or "pending")
    kind = str(info.get("task_kind", task.get("task_kind", "config")) or "config")
    run_index = _latest_run_index(task, info)
    config_file = resolve_task_config_file({**task, **info}, kind, task_dir)
    payload_path = os.path.join(task_dir, config_file) if config_file else ""
    latest_log = _log_path(task, run_index=run_index) if run_index > 0 else ""
    pids = info.get("pids", task.get("pids", [])) or []
    latest_pid = next((pid for pid in reversed(pids) if pid), None)

    record: dict[str, Any] = {
        "name": str(task.get("name", "") or os.path.basename(task_dir)),
        "status": status,
        "kind": kind,
        "created_at": info.get("created_at", task.get("created_at", "")),
        "run_index": run_index,
        "pid": latest_pid,
        "directory": _normalized_path(task_dir),
        "payload": _normalized_path(payload_path) if payload_path else None,
        "latest_log": _normalized_path(latest_log) if latest_log and os.path.isfile(latest_log) else None,
    }
    if detailed:
        command_argv = info.get("cmd", task.get("cmd"))
        command_text = (
            shlex.join(str(part) for part in command_argv)
            if isinstance(command_argv, list)
            else str(task.get("config_text", "") or "").strip()
        )
        record.update(
            {
                "progress": info.get("progress", task.get("progress", 0.0)),
                "pinned": bool(info.get("pinned", task.get("pinned", False))),
                "start_times": info.get("start_times", task.get("start_times", [])) or [],
                "finish_times": info.get("finish_times", task.get("finish_times", [])) or [],
                "pids": pids,
                "durations": info.get("durations", task.get("durations", [])) or [],
                "exit_codes": info.get("exit_codes", task.get("exit_codes", [])) or [],
                "source_states": info.get("source_states", task.get("source_states", [])) or [],
                "records": info.get("records", task.get("records", [])) or [],
                "tracks": info.get("tracks", task.get("tracks", [])) or [],
                "env": info.get("env", task.get("env", {})) or {},
                "notes": info.get("notes", task.get("notes", "")) or "",
                "config": task.get("config", {}) or {},
                "command": command_text or None,
                "command_mode": info.get("command_mode", task.get("command_mode")),
                "command_argv": command_argv if isinstance(command_argv, list) else None,
                "workdir": info.get("workdir", task.get("workdir")),
                "shell_kind": info.get("shell_kind", task.get("shell_kind")),
                "load_error": task.get("_load_error"),
            }
        )
        if selected_run is not None:
            record["selected_run"] = _selected_run_record(task, selected_run)
    return record


def _print_human_task_table(records: list[dict[str, Any]]) -> None:
    if not records:
        print("No tasks found.")
        return
    status_width = max(6, max(len(str(item["status"])) for item in records))
    name_width = max(4, min(48, max(len(str(item["name"])) for item in records)))
    print(f"{'STATUS':<{status_width}}  {'NAME':<{name_width}}  CREATED")
    for item in records:
        print(
            f"{str(item['status']):<{status_width}}  "
            f"{str(item['name']):<{name_width}}  "
            f"{str(item.get('created_at', ''))}"
        )


def _print_task_result(context: Any, records: list[dict[str, Any]]) -> None:
    if context.json_output:
        _json_dump({"tasks": records})
        return
    for record in records:
        print(f"{record['status']}\t{record['name']}")


def _parse_env(items: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise CliUsageError(f"environment value must use KEY=VALUE: {item}")
        key, value = item.split("=", 1)
        if not is_valid_environment_name(key):
            raise CliUsageError(f"invalid environment variable name: {key}")
        if "\x00" in value:
            raise CliUsageError(f"environment variable '{key}' contains a null byte")
        env[key] = value
    return env


def _load_env_files(paths: list[str], *, base_dir: str) -> dict[str, str]:
    """Load simple dotenv-style files, with later files taking precedence."""

    env: dict[str, str] = {}
    for item in paths:
        requested = os.path.expanduser(str(item))
        path = requested if os.path.isabs(requested) else os.path.join(base_dir, requested)
        display_path = _normalized_path(path)
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                lines = handle.readlines()
        except FileNotFoundError as exc:
            raise CliUsageError(f"environment file not found: {display_path}") from exc
        except (OSError, UnicodeError) as exc:
            raise CliUsageError(f"unable to read environment file {display_path}: {exc}") from exc

        for line_number, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise CliUsageError(
                    f"environment file {display_path}:{line_number} must use KEY=VALUE"
                )
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not is_valid_environment_name(key):
                raise CliUsageError(
                    f"invalid environment variable name in {display_path}:{line_number}: {key}"
                )
            if "\x00" in value:
                raise CliUsageError(
                    f"environment variable value contains a null byte in {display_path}:{line_number}: {key}"
                )
            env[key] = value
    return env


def _powershell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _windows_argv_quote(value: str, *, force: bool = False) -> str:
    """Quote one argument for the Windows C command-line parser."""

    value = str(value)
    needs_quotes = force or not value or " " in value or "\t" in value
    if not needs_quotes and '"' not in value:
        return value

    result = ['"'] if needs_quotes else []
    backslashes = 0
    for char in value:
        if char == "\\":
            backslashes += 1
            continue
        if char == '"':
            result.append("\\" * (backslashes * 2 + 1))
            result.append('"')
        else:
            if backslashes:
                result.append("\\" * backslashes)
            result.append(char)
        backslashes = 0
    if backslashes:
        result.append("\\" * (backslashes * (2 if needs_quotes else 1)))
    if needs_quotes:
        result.append('"')
    return "".join(result)


def _cmd_quote(value: str) -> str:
    """Quote one literal argv item stored inside a Windows batch file."""

    value = str(value)
    if "\r" in value or "\n" in value:
        raise CliUsageError("cmd cannot preserve newlines inside exact command arguments")
    value = value.replace("%", "%%")
    return _windows_argv_quote(value, force=any(char in _CMD_META_CHARS for char in value))

def _render_argument_command(parts: list[str], workspace: str) -> str:
    runtime = get_shell_runtime_for_workspace(workspace)
    kind = str(runtime.get("terminal_kind", "") or "").lower()
    if kind == "powershell":
        return "& " + " ".join(_powershell_quote(part) for part in parts)
    if kind == "cmd":
        return " ".join(_cmd_quote(part) for part in parts)
    return shlex.join(parts)


def _resolve_exec_script_path(parts: list[str]) -> str | None:
    """Validate and resolve a directly invoked shell script path."""

    extension = os.path.splitext(parts[0])[1].lower()
    if extension not in _SHELL_SCRIPT_EXTENSIONS:
        return None

    script_path = os.path.abspath(os.path.expanduser(parts[0]))
    if not os.path.isfile(script_path):
        resolved = shutil.which(parts[0]) if not os.path.dirname(parts[0]) else None
        if resolved and os.path.isfile(resolved):
            script_path = os.path.abspath(resolved)
        else:
            raise CliError(f"script file not found: {parts[0]}")
    return script_path


def _build_exec_argv(
    parts: list[str],
    workspace: str,
    source_script: str | None,
) -> list[str]:
    """Return the exact process argv persisted for an exec task."""

    if not source_script:
        return [str(part) for part in parts]

    try:
        return build_script_file_argv(source_script, parts[1:], workspace)
    except (RuntimeError, ValueError) as exc:
        raise CliError(str(exc)) from exc


def _wait_for_task_records(
    tasks: list[dict[str, Any]],
    *,
    timeout: float = 0.0,
    require_started: bool = False,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout if timeout > 0 else None
    while True:
        records = [_task_record(task) for task in tasks]
        statuses = [str(record["status"]) for record in records]
        if require_started and any(status == "pending" for status in statuses):
            raise CliError("one or more tasks were not accepted by a runner")
        if all(status in _FINAL_STATUSES for status in statuses):
            return records
        if deadline is not None and time.monotonic() >= deadline:
            active = [record["name"] for record in records if record["status"] in _ACTIVE_STATUSES]
            raise CliError("timed out waiting for: " + ", ".join(active))
        time.sleep(0.1)


def _write_available_log(path: str, offset: int) -> int:
    while True:
        content, next_offset = safe_read_log(path, offset, max_bytes=65536)
        if not content or next_offset == offset:
            return offset
        try:
            sys.stdout.write(content)
        except UnicodeEncodeError:
            encoding = str(getattr(sys.stdout, "encoding", "") or "utf-8")
            safe_content = content.encode(encoding, errors="replace").decode(encoding)
            sys.stdout.write(safe_content)
        sys.stdout.flush()
        offset = next_offset


def _follow_task(task: dict[str, Any]) -> dict[str, Any]:
    current_path = _log_path(task)
    offset = 0
    while True:
        latest_record = _task_record(task)
        next_path = _log_path(task)
        if next_path != current_path:
            current_path = next_path
            offset = 0
        offset = _write_available_log(current_path, offset)
        if latest_record["status"] in _FINAL_STATUSES:
            stable_reads = 0
            deadline = time.monotonic() + 0.5
            while stable_reads < 3 and time.monotonic() < deadline:
                next_offset = _write_available_log(current_path, offset)
                if next_offset == offset:
                    stable_reads += 1
                    time.sleep(0.05)
                else:
                    offset = next_offset
                    stable_reads = 0
            return latest_record
        time.sleep(0.1)


def _submit_and_wait(
    context: Any,
    manager: TaskManager,
    tasks: list[dict[str, Any]],
    *,
    mode: str,
    workers: int,
    detach: bool,
) -> int:
    active = [task["name"] for task in tasks if str(task.get("status", "")) in _ACTIVE_STATUSES]
    if active:
        raise CliError("task already active: " + ", ".join(active))
    broken = [task for task in tasks if task.get("_load_error")]
    if broken:
        raise CliError(
            "; ".join(f"{task['name']}: {task['_load_error']}" for task in broken)
        )

    names = [str(task["name"]) for task in tasks]
    if not submit_cli_tasks(
        manager,
        names,
        execution_mode=mode,
        max_workers=max(1, workers),
    ):
        raise CliError("runner did not accept all requested tasks")

    if detach:
        records = [_task_record(task) for task in tasks]
        if context.json_output:
            _json_dump({"submitted": True, "tasks": records})
        else:
            for name in names:
                print(name)
        return 0

    if len(tasks) == 1 and not context.json_output:
        record = _follow_task(tasks[0])
        _eprint(f"{_program(context)}: {record['name']} {record['status']}")
        return 0 if record["status"] == "completed" else 1

    records = _wait_for_task_records(tasks, require_started=True)
    _print_task_result(context, records)
    return 0 if all(record["status"] == "completed" for record in records) else 1


def _resolve_config(workspace: str, value: str) -> str:
    info = load_script_info(workspace)
    script_path = str(info.get("script_path", "") or "")
    script_dir = os.path.dirname(script_path) if script_path else os.getcwd()
    resolved = resolve_config_path(value, script_dir)
    if not resolved or not os.path.isfile(resolved):
        raise CliError(f"configuration not found: {value}")
    return os.path.abspath(resolved)


def _load_task_config_batch(
    workspace: str,
    config_path: str,
    name_prefix: str | None,
) -> tuple[str, list[dict[str, Any]], str, list[str]]:
    """Read and validate one config batch without creating task directories."""

    if _workspace_kind(workspace) != WORKSPACE_KIND_SCRIPT:
        raise CliUsageError("add and run --from require a Python script workspace")
    resolved = _resolve_config(workspace, config_path)
    try:
        config = load_yaml_strict(resolved)
        configs = generate_batch_configs(config)
    except Exception as exc:
        raise CliError(f"cannot load configuration '{config_path}': {exc}") from exc

    prefix = name_prefix or safe_filename(os.path.splitext(os.path.basename(resolved))[0])
    total = len(configs)
    names = [
        f"{prefix}_[{index}-of-{total}]" if total > 1 else prefix
        for index in range(1, total + 1)
    ]
    for name in names:
        name_error = validate_task_name(name)
        if name_error:
            raise CliUsageError(name_error)
    return resolved, configs, prefix, names


def _task_name_plan(tasks_dir: str, requested_name: str, *, exact: bool) -> dict[str, Any]:
    """Describe whether the next task can retain its requested name."""

    available = not os.path.exists(os.path.join(tasks_dir, requested_name))
    return {
        "requested_name": requested_name,
        "planned_name": requested_name if available else None,
        "name_is_exact": bool(exact and available),
        "name_available": available,
    }


def _create_tasks(
    manager: TaskManager,
    workspace: str,
    config_path: str,
    name_prefix: str | None,
) -> list[dict[str, Any]]:
    _resolved, configs, prefix, _names = _load_task_config_batch(
        workspace,
        config_path,
        name_prefix,
    )
    generator = TaskGenerator(root_dir=manager.tasks_dir)
    try:
        tasks = generator.create_tasks(configs, prefix)
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    manager.add_tasks(tasks)
    return tasks


def cmd_init(context: Any, args: Any) -> int:
    if args.config and not args.script:
        raise CliUsageError("--config requires SCRIPT")
    if args.script:
        try:
            workspace = bootstrap_workspace(
                args.script,
                args.config,
                preserve_default=not bool(args.config),
            )
        except (FileNotFoundError, ValueError) as exc:
            raise CliError(str(exc)) from exc
    else:
        project_root = _normalized_path(os.path.join(os.getcwd(), DEFAULT_ROOT_NAME))
        os.makedirs(project_root, exist_ok=True)
        workspace = bootstrap_shell_workspace(project_root)
    summary = {
        "workspace": _normalized_path(workspace),
        "kind": _workspace_kind(workspace),
        "name": _workspace_label(workspace),
    }
    if context.json_output:
        _json_dump(summary)
    else:
        print(summary["workspace"])
    return 0


def cmd_exec(context: Any, args: Any) -> int:
    parts = list(args.command_argv or [])
    has_separator = bool(parts and parts[0] == "--")
    if has_separator:
        parts = parts[1:]
    shell_command = args.shell_command
    if shell_command is not None and parts:
        raise CliUsageError("-c/--command accepts exactly one command string")
    if shell_command is None and parts and not has_separator:
        raise CliUsageError("exec argv form requires '--' before COMMAND")
    if shell_command is None and not parts:
        raise CliUsageError("exec requires COMMAND after '--' or -c/--command COMMAND_STRING")

    env = _load_env_files(
        list(args.env_file or []),
        base_dir=os.path.abspath(context.directory),
    )
    env.update(_parse_env(list(args.env or [])))
    requested_name = str(args.name or "command").strip()
    name_error = validate_task_name(requested_name)
    if name_error:
        raise CliUsageError(name_error)

    source_script: str | None = None
    uses_shell_command = shell_command is not None
    if uses_shell_command:
        command_text = str(shell_command)
        if not command_text.strip():
            raise CliUsageError("-c/--command cannot be empty")
    else:
        if not str(parts[0]).strip():
            raise CliUsageError("exec command cannot be empty")
        source_script = _resolve_exec_script_path(parts)

    dry_run = bool(args.dry_run)
    workspace = _shell_workspace_for_exec(context, create=not dry_run)
    command_argv: list[str] | None = None
    shell_executable: str | None = None
    shell_kind: str | None = None
    if uses_shell_command:
        runtime = get_shell_runtime_for_workspace(workspace)
        shell_executable = str(runtime.get("executable", "") or "").strip()
        shell_kind = str(runtime.get("terminal_kind", "") or "").strip().lower()
        if not shell_executable or not bool(runtime.get("available", False)):
            raise CliError("unable to resolve an available shell for -c/--command")
    else:
        command_argv = _build_exec_argv(parts, workspace, source_script)
        command_text = _render_argument_command(command_argv, workspace)

    if dry_run:
        tasks_dir = os.path.join(workspace, TASKS_DIR)
        task_plan = _task_name_plan(
            tasks_dir,
            requested_name,
            exact=args.name is not None,
        )
        if args.name is not None and not task_plan["name_available"]:
            raise CliError(f"Task name '{requested_name}' already exists in the current workspace")
        workspace_exists = os.path.isfile(os.path.join(workspace, SCRIPT_INFO_FILENAME))
        payload = {
            "dry_run": True,
            "operation": "exec",
            "workspace": _normalized_path(workspace),
            "workspace_exists": workspace_exists,
            "creates_workspace": not workspace_exists,
            "task": task_plan,
            "workdir": _normalized_path(context.directory),
            "command_mode": "shell" if uses_shell_command else "argv",
            "command_argv": command_argv,
            "shell_expression": command_text if uses_shell_command else None,
            "shell_executable": shell_executable or None,
            "shell_kind": shell_kind or None,
            "script": _normalized_path(source_script) if source_script else None,
            "env": env,
            "detach": bool(args.detach),
        }
        if context.json_output:
            _json_dump(payload)
        else:
            workspace_note = "will be created" if payload["creates_workspace"] else "exists"
            print("Dry run:    exec")
            print(f"Workspace:  {payload['workspace']} ({workspace_note})")
            if task_plan["planned_name"]:
                name_note = "exact" if task_plan["name_is_exact"] else "automatic"
                print(f"Task:       {task_plan['planned_name']} ({name_note})")
            else:
                print(f"Task:       {requested_name} (a unique suffix will be added)")
            print(f"Mode:       {payload['command_mode']}")
            print(f"Command:    {command_text}")
            print(f"Workdir:    {payload['workdir']}")
            print("Result:     nothing was created or run")
        return 0

    with _task_manager(workspace, lazy_scan=None) as manager:
        if args.name is not None:
            name_error = validate_task_name(requested_name, manager.tasks_dir)
            if name_error:
                raise CliError(name_error)
        generator = TaskGenerator(root_dir=manager.tasks_dir)
        try:
            task = generator.create_shell_task(
                requested_name,
                command_text.rstrip() + "\n",
                command_mode="shell" if uses_shell_command else "argv",
                command_argv=command_argv,
                workdir=context.directory,
                shell_executable=shell_executable,
                shell_kind=shell_kind,
                env=env,
                script_path=source_script,
            )
        except ValueError as exc:
            raise CliError(str(exc)) from exc
        manager.add_task(task)
        _eprint(f"{_program(context)}: created {task['name']}")
        return _submit_and_wait(
            context,
            manager,
            [task],
            mode="thread",
            workers=1,
            detach=bool(args.detach),
        )


def cmd_add(context: Any, args: Any, manager: TaskManager, workspace: str) -> int:
    tasks = _create_tasks(manager, workspace, args.config, args.name)
    records = [_task_record(task) for task in tasks]
    if context.json_output:
        _json_dump({"created": records})
    else:
        for task in tasks:
            print(task["name"])
    return 0


def cmd_run(context: Any, args: Any, manager: TaskManager, workspace: str) -> int:
    if args.from_config and args.tasks:
        raise CliUsageError("run accepts either exact TASK names or --from CONFIG, not both")
    if args.name and not args.from_config:
        raise CliUsageError("--name is only valid together with --from")
    if args.from_config:
        tasks = _create_tasks(manager, workspace, args.from_config, args.name)
        _eprint(f"{_program(context)}: created {len(tasks)} task(s)")
    else:
        if not args.tasks:
            raise CliUsageError("run requires at least one TASK or --from CONFIG")
        tasks = _resolve_exact_tasks(manager, list(args.tasks))
    return _submit_and_wait(
        context,
        manager,
        tasks,
        mode=args.mode,
        workers=args.workers,
        detach=bool(args.detach),
    )


def cmd_run_dry_run(context: Any, args: Any, workspace: str) -> int:
    """Preview ``run --from`` without initializing a manager or writing files."""

    if not args.from_config:
        raise CliUsageError("run --dry-run requires --from CONFIG")
    if args.tasks:
        raise CliUsageError("run accepts either exact TASK names or --from CONFIG, not both")
    resolved, configs, _prefix, requested_names = _load_task_config_batch(
        workspace,
        args.from_config,
        args.name,
    )
    tasks_dir = os.path.join(workspace, TASKS_DIR)
    tasks = [
        _task_name_plan(tasks_dir, name, exact=False)
        for name in requested_names
    ]
    payload = {
        "dry_run": True,
        "operation": "run-from",
        "workspace": _normalized_path(workspace),
        "workspace_exists": os.path.isfile(os.path.join(workspace, SCRIPT_INFO_FILENAME)),
        "creates_workspace": False,
        "config": _normalized_path(resolved),
        "task_count": len(configs),
        "tasks": tasks,
        "mode": args.mode,
        "workers": args.workers,
        "detach": bool(args.detach),
    }
    if context.json_output:
        _json_dump(payload)
    else:
        print("Dry run:    run --from")
        print(f"Workspace:  {payload['workspace']}")
        print(f"Config:     {payload['config']}")
        print(f"Tasks:      {payload['task_count']}")
        for task in tasks:
            if task["planned_name"]:
                print(f"  {task['planned_name']}")
            else:
                print(f"  {task['requested_name']} (a unique suffix will be added)")
        print(f"Execution:  {args.mode}, {args.workers} worker(s)")
        print("Result:     nothing was created or run")
    return 0


def _trash_records(manager: TaskManager) -> list[dict[str, Any]]:
    trash_dir = Path(manager.tasks_dir) / TRASH_DIR
    if not trash_dir.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(trash_dir.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_dir():
            continue
        info = load_task_info(str(path)) or {}
        records.append(
            {
                "name": str(info.get("name", "") or path.name),
                "trash_name": path.name,
                "status": str(info.get("status", "unknown") or "unknown"),
                "created_at": info.get("created_at", ""),
                "directory": _normalized_path(str(path)),
            }
        )
    return records


def cmd_ls(context: Any, args: Any, manager: TaskManager) -> int:
    if args.trash:
        records = _trash_records(manager)
        query = str(args.query or "").lower()
        statuses = set(args.status or [])
        if statuses:
            records = [record for record in records if str(record.get("status", "")) in statuses]
        if query:
            records = [
                record
                for record in records
                if query in str(record.get("name", "")).lower()
                or query in str(record.get("trash_name", "")).lower()
            ]
        if args.sort == "name":
            records.sort(key=lambda item: str(item.get("name", "")).lower())
        elif args.sort == "status":
            records.sort(
                key=lambda item: (
                    str(item.get("status", "")),
                    str(item.get("name", "")).lower(),
                )
            )
        else:
            records.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        if args.reverse:
            records.reverse()
    else:
        tasks = _refresh_tasks(manager)
        query = str(args.query or "").lower()
        statuses = set(args.status or [])
        if statuses:
            tasks = [task for task in tasks if str(task.get("status", "pending")) in statuses]
        if query:
            tasks = [
                task
                for task in tasks
                if query in str(task.get("name", "")).lower()
                or query in str(task.get("search_text", "")).lower()
            ]
        if args.sort == "name":
            tasks.sort(key=lambda item: str(item.get("name", "")).lower())
        elif args.sort == "status":
            tasks.sort(
                key=lambda item: (
                    str(item.get("status", "")),
                    str(item.get("name", "")).lower(),
                )
            )
        else:
            tasks.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        if args.reverse:
            tasks.reverse()
        records = [_task_record(task) for task in tasks]

    if args.limit is not None:
        records = records[: args.limit]
    if context.json_output:
        _json_dump({"tasks": records, "count": len(records), "trash": bool(args.trash)})
    else:
        _print_human_task_table(records)
    return 0


def cmd_status(context: Any, manager: TaskManager, workspace: str) -> int:
    tasks = _refresh_tasks(manager)
    counts = {status: 0 for status in sorted(_VALID_STATUSES)}
    for task in tasks:
        status = str(task.get("status", "pending") or "pending")
        counts[status] = counts.get(status, 0) + 1
    info = load_script_info(workspace)
    active = [
        str(task.get("name", ""))
        for task in tasks
        if str(task.get("status", "")) in _ACTIVE_STATUSES
    ]
    payload = {
        "workspace": _normalized_path(workspace),
        "name": _workspace_label(workspace),
        "kind": _workspace_kind(workspace),
        "script": info.get("script_path") or None,
        "total": len(tasks),
        "counts": counts,
        "active": active,
    }
    if context.json_output:
        _json_dump(payload)
    else:
        print(f"Workspace: {payload['workspace']}")
        print(f"Kind:      {payload['kind']}")
        if payload["script"]:
            print(f"Script:    {payload['script']}")
        print(f"Tasks:     {payload['total']}")
        print(
            "Status:    "
            + "  ".join(f"{status}={counts.get(status, 0)}" for status in sorted(_VALID_STATUSES))
        )
        if active:
            print("Active:    " + ", ".join(active))
    return 0


def cmd_show(context: Any, args: Any, manager: TaskManager) -> int:
    task, reference_run = _resolve_task_run_reference(manager, args.task)
    if reference_run is not None and args.run is not None:
        raise CliUsageError("TASK@RUN cannot be combined with --run")
    selected_run = reference_run if reference_run is not None else args.run
    record = _task_record(task, detailed=True, selected_run=selected_run)
    if context.json_output:
        _json_dump(record)
        return 0
    print(f"Name:       {record['name']}")
    print(f"Status:     {record['status']}")
    print(f"Kind:       {record['kind']}")
    print(f"Created:    {record['created_at']}")
    print(f"Directory:  {record['directory']}")
    print(f"Payload:    {record['payload'] or '-'}")
    print(f"Run index:  {record['run_index']}")
    print(f"PID:        {record['pid'] or '-'}")
    print(f"Latest log: {record['latest_log'] or '-'}")
    if selected_run is not None:
        run = record["selected_run"]
        print(f"Selected:   run {run['index']}")
        print(f"Started:    {run['start_time'] or '-'}")
        print(f"Finished:   {run['finish_time'] or '-'}")
        print(f"Run PID:    {run['pid'] or '-'}")
        duration = run["duration_seconds"]
        print(f"Duration:   {duration:.3f}s" if duration is not None else "Duration:   -")
        exit_code = run["exit_code"]
        print(f"Exit code:  {exit_code}" if exit_code is not None else "Exit code:  -")
        print(f"Source:     {run['source_state'] or '-'}")
        print(f"Run log:    {run['log'] or '-'}")
    if record["command"]:
        print(f"Command:    {record['command']}")
    if record["workdir"]:
        print(f"Workdir:    {_normalized_path(record['workdir'])}")
    if record["shell_kind"]:
        print(f"Shell:      {record['shell_kind']}")
    if record["config"]:
        print("Config:")
        print(yaml.safe_dump(record["config"], allow_unicode=True, sort_keys=False).rstrip())
    if record["env"]:
        print("Environment:")
        for key, value in sorted(record["env"].items()):
            print(f"  {key}={value}")
    if record["notes"]:
        print(f"Notes:      {record['notes']}")
    if record["load_error"]:
        print(f"Load error: {record['load_error']}")
    return 0


def cmd_log(context: Any, args: Any, manager: TaskManager) -> int:
    task, reference_run = _resolve_task_run_reference(manager, args.task)
    if reference_run is not None and args.run is not None:
        raise CliUsageError("TASK@RUN cannot be combined with --run")
    selected_run = reference_run if reference_run is not None else args.run
    if args.follow and selected_run is not None:
        raise CliUsageError("log accepts either --follow or --run, not both")
    if args.follow and args.path:
        raise CliUsageError("log accepts either --follow or --path, not both")
    if context.json_output and not args.path:
        raise CliUsageError("--json is only supported by log together with --path")
    if selected_run is not None:
        _selected_run_record(task, selected_run)
    if args.follow and str(task.get("status", "pending")) == "pending":
        raise CliError(f"cannot follow pending task: {task['name']}")
    path = _log_path(task, run_index=selected_run)
    if args.path:
        payload = {
            "task": task["name"],
            "run": selected_run or _latest_run_index(task),
            "path": _normalized_path(path),
        }
        if context.json_output:
            _json_dump(payload)
        else:
            print(payload["path"])
        return 0

    if not args.follow:
        if not os.path.isfile(path):
            raise CliError(f"log does not exist: {_normalized_path(path)}")
        _write_available_log(path, 0)
        return 0

    record = _follow_task(task)
    return 0 if record["status"] == "completed" else 1


def cmd_wait(context: Any, args: Any, manager: TaskManager) -> int:
    tasks = _resolve_exact_tasks(manager, list(args.tasks))
    pending = [task["name"] for task in tasks if str(task.get("status", "")) == "pending"]
    if pending:
        raise CliError("cannot wait for pending tasks: " + ", ".join(pending))
    records = _wait_for_task_records(tasks, timeout=float(args.timeout))
    _print_task_result(context, records)
    return 0 if all(record["status"] == "completed" for record in records) else 1


def cmd_stop(context: Any, args: Any, manager: TaskManager) -> int:
    tasks = _resolve_exact_tasks(manager, list(args.tasks))
    inactive = [task["name"] for task in tasks if str(task.get("status", "")) not in _ACTIVE_STATUSES]
    if inactive:
        raise CliError("task is not active: " + ", ".join(inactive))

    requested: list[dict[str, Any]] = []
    for task in tasks:
        if not manager.request_task_cancel(task["name"]):
            raise CliError(f"could not request cancellation for '{task['name']}'")
        requested.append(task)
    records = _wait_for_task_records(requested, timeout=float(args.timeout))
    if context.json_output:
        _json_dump({"stopped": records})
    else:
        for record in records:
            print(record["name"])
    return 0 if all(record["status"] == "cancelled" for record in records) else 1


def cmd_rm(context: Any, args: Any, manager: TaskManager) -> int:
    tasks = _resolve_exact_tasks(manager, list(args.tasks))
    active = [task["name"] for task in tasks if str(task.get("status", "")) in _ACTIVE_STATUSES]
    if active:
        raise CliError("cannot remove active tasks; stop them first: " + ", ".join(active))
    names = [str(task["name"]) for task in tasks]
    deleted = manager.delete_tasks(names)
    if set(deleted) != set(names):
        skipped = [name for name in names if name not in set(deleted)]
        raise CliError("could not remove: " + ", ".join(skipped))
    if context.json_output:
        _json_dump({"removed": deleted})
    else:
        for name in deleted:
            print(name)
    return 0


def cmd_restore(context: Any, args: Any, manager: TaskManager) -> int:
    records = _trash_records(manager)
    selected: list[dict[str, Any]] = []
    seen_trash_names: set[str] = set()
    for name in args.tasks:
        matches = [
            record
            for record in records
            if record["name"] == name or record["trash_name"] == name
        ]
        if not matches:
            raise CliError(f"trashed task not found: {name}")
        if len(matches) > 1:
            raise CliError(f"trashed task name is ambiguous: {name}; use its exact trash_name")
        record = matches[0]
        trash_name = str(record["trash_name"])
        if trash_name not in seen_trash_names:
            seen_trash_names.add(trash_name)
            selected.append(record)

    restore_plan: list[tuple[dict[str, Any], str, str]] = []
    target_names: set[str] = set()
    tasks_root = os.path.abspath(manager.tasks_dir)
    for record in selected:
        target_name = str(record["name"])
        name_error = validate_existing_task_name(target_name)
        if name_error:
            raise CliError(f"cannot restore '{target_name}': {name_error}")
        if target_name in target_names:
            raise CliError(f"cannot restore '{target_name}': multiple trash entries have that name")
        target_names.add(target_name)
        source = str(record["directory"])
        destination = os.path.abspath(os.path.join(tasks_root, target_name))
        try:
            inside_tasks_root = os.path.commonpath([tasks_root, destination]) == tasks_root
        except ValueError:
            inside_tasks_root = False
        if not inside_tasks_root or os.path.dirname(destination) != tasks_root:
            raise CliError(f"cannot restore '{target_name}': invalid task destination")
        if not os.path.isdir(source):
            raise CliError(f"cannot restore '{target_name}': trash entry no longer exists")
        if os.path.exists(destination):
            raise CliError(f"cannot restore '{target_name}': an active task has that name")
        restore_plan.append((record, source, destination))

    restored: list[str] = []
    for record, source, destination in restore_plan:
        target_name = str(record["name"])
        try:
            shutil.move(source, destination)
            update_task_info(
                destination,
                lambda info, target_name=target_name: info.update({"name": target_name}),
                raise_error=True,
            )
        except Exception as exc:
            raise CliError(f"could not restore '{target_name}': {exc}") from exc
        restored.append(target_name)
    manager.scan_disk()
    if context.json_output:
        _json_dump({"restored": restored})
    else:
        for name in restored:
            print(name)
    return 0


def cmd_mv(context: Any, args: Any, manager: TaskManager) -> int:
    _resolve_exact_tasks(manager, [args.task])
    ok, result = manager.rename_task(args.task, args.new_name)
    if not ok:
        raise CliError(str(result))
    if context.json_output:
        _json_dump({"old_name": args.task, "name": result})
    else:
        print(result)
    return 0


def cmd_pin(context: Any, args: Any, manager: TaskManager) -> int:
    tasks = _resolve_exact_tasks(manager, list(args.tasks))
    pinned = not bool(args.off)
    changed: list[str] = []
    for task in tasks:
        ok, result = manager.set_task_pinned(task["name"], pinned)
        if not ok:
            raise CliError(f"{task['name']}: {result}")
        changed.append(task["name"])
    if context.json_output:
        _json_dump({"pinned": pinned, "tasks": changed})
    else:
        for name in changed:
            print(name)
    return 0


def cmd_export(context: Any, args: Any, manager: TaskManager) -> int:
    export_format = args.format or (
        "json" if context.json_output and args.output == "-" else "csv"
    )
    if context.json_output and args.output == "-" and export_format != "json":
        raise CliUsageError("--json with stdout export requires '--format json'")
    if args.tasks:
        tasks = _resolve_exact_tasks(manager, list(args.tasks))
    else:
        tasks = _refresh_tasks(manager)
    statuses = set(args.status or [])
    if statuses:
        tasks = [task for task in tasks if str(task.get("status", "pending")) in statuses]
    content = build_export_json(tasks) if export_format == "json" else build_export_csv(tasks)
    if not content:
        content = "[]" if export_format == "json" else ""
    if args.output == "-":
        sys.stdout.write(content)
        if content and not content.endswith("\n"):
            sys.stdout.write("\n")
        return 0
    output = os.path.abspath(os.path.expanduser(os.path.expandvars(args.output)))
    try:
        with open(output, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
    except OSError as exc:
        raise CliError(f"cannot write export '{args.output}': {exc}") from exc
    if context.json_output:
        _json_dump({"output": _normalized_path(output), "format": export_format, "tasks": len(tasks)})
    else:
        print(_normalized_path(output))
    return 0


_SETTING_CHOICES: dict[str, set[str]] = {
    "generator_mode": {"form", "yaml"},
    "manager_execution_mode": {"thread", "process"},
    "log_level": {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
    "shell_mode": {"follow", "custom"},
    "gpu_scheduler_task_mode": {"single", "multi"},
    "gpu_scheduler_selection_mode": {"auto", "specified"},
}
_SETTING_MINIMUMS: dict[str, float] = {
    "header_refresh_interval": 1,
    "generator_form_columns": 1,
    "manager_columns": 1,
    "manager_max_workers": 1,
    "ui_page_size": 0,
    "monitor_chunk_size": 1,
    "monitor_scrollback": 0,
    "monitor_line_height": 1e-12,
    "monitor_sidebar_width_pct": 1,
    "gpu_scheduler_gpus_per_task": 1,
    "gpu_scheduler_min_free_memory_gb": 0,
    "gpu_scheduler_stable_seconds": 0,
    "gpu_scheduler_max_wait_seconds": 0,
    "gpu_scheduler_max_tasks_per_gpu": 1,
}
_SETTING_PERCENTAGES = {
    "gpu_scheduler_memory_used_pct",
    "gpu_scheduler_compute_used_pct",
}


def _validate_setting_value(key: str, value: Any) -> Any:
    if key not in SETTINGS_DEFAULTS:
        raise CliError(f"unknown setting: {key}")
    default = SETTINGS_DEFAULTS[key]
    if not setting_numbers_are_finite(value):
        raise CliUsageError(f"{key} must contain only finite numbers")
    expected = type(default)
    if expected is bool:
        if not isinstance(value, bool):
            raise CliUsageError(f"{key} expects true or false")
    elif expected is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CliUsageError(f"{key} expects an integer")
    elif expected is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CliUsageError(f"{key} expects a number")
        value = float(value)
    elif expected is str:
        if not isinstance(value, str):
            raise CliUsageError(f"{key} expects a string")
    elif not isinstance(value, expected):
        raise CliUsageError(f"{key} expects {expected.__name__}")
    if key == "global_env":
        try:
            value = normalize_environment(value, drop_none_values=True)
        except ValueError as exc:
            raise CliUsageError(str(exc)) from exc
    if key == "log_level":
        value = value.upper()
    choices = _SETTING_CHOICES.get(key)
    if choices and value not in choices:
        raise CliUsageError(f"{key} expects one of: {', '.join(sorted(choices))}")
    if key == "ui_port" and not 1 <= value <= 65535:
        raise CliUsageError("ui_port must be between 1 and 65535")
    if key in _SETTING_MINIMUMS and value < _SETTING_MINIMUMS[key]:
        minimum = _SETTING_MINIMUMS[key]
        comparison = "greater than zero" if minimum > 0 else "zero or greater"
        raise CliUsageError(f"{key} must be {comparison}")
    if key in _SETTING_PERCENTAGES and not 0 <= value <= 100:
        raise CliUsageError(f"{key} must be between 0 and 100")
    if key == "monitor_sidebar_width_pct" and value > 100:
        raise CliUsageError("monitor_sidebar_width_pct must be between 1 and 100")
    return value


def cmd_config(context: Any, args: Any, workspace: str) -> int:
    action = args.config_action
    if not action:
        raise CliUsageError("config requires an action: list, get, set, unset, or path")
    settings_path = _settings_path(workspace)
    ensure_settings_file(workspace)
    if action == "path":
        normalized = _normalized_path(settings_path)
        if context.json_output:
            _json_dump({"path": normalized})
        else:
            print(normalized)
        return 0
    if action == "list":
        values = reload_settings(workspace)
        if context.json_output:
            _json_dump(values)
        else:
            print(yaml.safe_dump(values, allow_unicode=True, sort_keys=True).rstrip())
        return 0
    if args.key not in SETTINGS_DEFAULTS:
        raise CliError(f"unknown setting: {args.key}")
    if action == "get":
        value = reload_settings(workspace)[args.key]
        if context.json_output:
            _json_dump({args.key: value})
        elif isinstance(value, (dict, list)):
            print(yaml.safe_dump(value, allow_unicode=True, sort_keys=False).rstrip())
        else:
            print(str(value).lower() if isinstance(value, bool) else value)
        return 0
    if action == "set":
        try:
            value = yaml.safe_load(args.value)
        except yaml.YAMLError as exc:
            raise CliUsageError(f"invalid YAML value: {exc}") from exc
        value = _validate_setting_value(args.key, value)
    elif action == "unset":
        value = copy.deepcopy(SETTINGS_DEFAULTS[args.key])
    else:
        raise CliUsageError(f"unknown config action: {action}")
    save_setting_for_root(workspace, args.key, value)
    saved = reload_settings(workspace).get(args.key)
    if saved != value:
        raise CliError(f"setting was not persisted: {args.key}")
    if context.json_output:
        _json_dump({args.key: saved})
    elif isinstance(saved, (dict, list)):
        print(yaml.safe_dump(saved, allow_unicode=True, sort_keys=False).rstrip())
    else:
        print(str(saved).lower() if isinstance(saved, bool) else saved)
    return 0


def cmd_metrics(context: Any) -> int:
    metrics = SystemMonitor().sample()
    memory = psutil.virtual_memory()
    payload = {
        "cpu_percent": float(metrics.get("cpu_percent", 0) or 0),
        "memory": {
            "percent": float(metrics.get("mem_percent", 0) or 0),
            "used_bytes": int(memory.used),
            "total_bytes": int(memory.total),
        },
        "gpus": metrics.get("gpus", []) or [],
    }
    if context.json_output:
        _json_dump(payload)
    else:
        print(f"CPU:    {payload['cpu_percent']:.1f}%")
        print(
            f"Memory: {payload['memory']['percent']:.1f}% "
            f"({payload['memory']['used_bytes']} / {payload['memory']['total_bytes']} bytes)"
        )
        if payload["gpus"]:
            for gpu in payload["gpus"]:
                print(
                    f"GPU {gpu.get('index', '?')}: "
                    f"util={float(gpu.get('util', 0) or 0):.1f}% "
                    f"memory={gpu.get('mem_used', 0)}/{gpu.get('mem_total', 0)} MB"
                )
        else:
            print("GPU:    none")
    return 0


def _browser_choice(args: Any) -> bool | None:
    if bool(args.browser):
        return True
    if bool(args.no_browser):
        return False
    return None


def _launch_ui(*, start_path: str, port: int | None, open_browser: bool | None) -> int:
    from pyruns.web.app import main as web_main

    sys.argv = [sys.argv[0]]
    web_main(start_path=start_path, port=port, open_browser=open_browser)
    return 0


def cmd_ui(context: Any, args: Any) -> int:
    if context.workspace:
        raise CliUsageError(
            "ui does not use -w/--workspace; pass WORKSPACE or SCRIPT.py after 'ui'"
        )
    target = str(args.target or "").strip()
    is_script = target.lower().endswith(".py")
    if args.config and not is_script:
        raise CliUsageError("--config requires SCRIPT.py")
    open_browser = _browser_choice(args)
    if target.lower() == "shell":
        project_root = _find_project_root() or _normalized_path(
            os.path.join(os.getcwd(), DEFAULT_ROOT_NAME)
        )
        workspace = _normalized_path(bootstrap_shell_workspace(project_root))
        os.environ[ENV_KEY_ROOT] = workspace
        mark_workspace_active(workspace)
        return _launch_ui(
            start_path="/",
            port=args.port,
            open_browser=open_browser,
        )
    if is_script:
        try:
            bootstrap_workspace(target, args.config)
        except (FileNotFoundError, ValueError) as exc:
            raise CliError(str(exc)) from exc
        return _launch_ui(start_path="/", port=args.port, open_browser=open_browser)

    if target:
        workspace = _resolve_workspace_selector(target, _find_project_root())
        os.environ[ENV_KEY_ROOT] = workspace
        mark_workspace_active(workspace)
        return _launch_ui(start_path="/", port=args.port, open_browser=open_browser)

    os.makedirs(os.path.join(os.getcwd(), DEFAULT_ROOT_NAME), exist_ok=True)
    return _launch_ui(
        start_path=launcher_query(),
        port=args.port,
        open_browser=open_browser,
    )


def cmd_dev(context: Any, args: Any) -> int:
    try:
        bootstrap_workspace(args.script, args.config)
    except (FileNotFoundError, ValueError) as exc:
        raise CliError(str(exc)) from exc
    command = [sys.executable, "-m", "pyruns.web.app"]
    if args.port is not None:
        command.extend(["--port", str(args.port)])
    if args.no_browser:
        command.append("--no-browser")
    elif args.browser:
        command.append("--browser")
    return subprocess.run(command, check=False).returncode


def dispatch(context: Any, args: Any) -> int:
    """Dispatch one already-parsed command."""

    handler = str(args.handler)
    if context.workspace and handler in {"init", "config", "metrics", "dev"}:
        raise CliUsageError(f"{handler} does not use -w/--workspace")
    if context.json_output and handler in {"ui", "dev"}:
        raise CliUsageError("--json is not supported by ui or dev")
    if handler == "init":
        return cmd_init(context, args)
    if handler == "exec":
        return cmd_exec(context, args)
    if handler == "metrics":
        return cmd_metrics(context)
    if handler == "ui":
        return cmd_ui(context, args)
    if handler == "dev":
        return cmd_dev(context, args)
    if handler == "config":
        project_root = _find_project_root()
        if not project_root:
            raise CliError("no Pyruns project found; run 'pyr init' first")
        return cmd_config(context, args, project_root)
    if handler == "run" and args.dry_run:
        if not args.from_config:
            raise CliUsageError("run --dry-run requires --from CONFIG")
        if args.tasks:
            raise CliUsageError("run accepts either exact TASK names or --from CONFIG, not both")

    workspace = resolve_workspace(context)

    if handler == "run" and args.dry_run:
        return cmd_run_dry_run(context, args, workspace)

    lazy_scan = None if handler in {"run"} else False
    with _task_manager(workspace, lazy_scan=lazy_scan) as manager:
        if handler == "add":
            return cmd_add(context, args, manager, workspace)
        if handler == "run":
            return cmd_run(context, args, manager, workspace)
        if handler == "ls":
            return cmd_ls(context, args, manager)
        if handler == "status":
            return cmd_status(context, manager, workspace)
        if handler == "show":
            return cmd_show(context, args, manager)
        if handler == "log":
            return cmd_log(context, args, manager)
        if handler == "wait":
            return cmd_wait(context, args, manager)
        if handler == "stop":
            return cmd_stop(context, args, manager)
        if handler == "rm":
            return cmd_rm(context, args, manager)
        if handler == "restore":
            return cmd_restore(context, args, manager)
        if handler == "mv":
            return cmd_mv(context, args, manager)
        if handler == "pin":
            return cmd_pin(context, args, manager)
        if handler == "export":
            return cmd_export(context, args, manager)
    raise CliUsageError(f"unknown command: {handler}")
