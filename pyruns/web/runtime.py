"""Shared runtime state for the Pyruns API server."""

from __future__ import annotations

import json
import math
import os
import shutil
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from concurrent.futures import CancelledError, Future
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, BinaryIO, Callable, Dict, List

import yaml
from omegaconf import DictConfig

import pyruns._config as _cfg
from pyruns._config import (
    CONFIG_FILENAME,
    CONFIG_DEFAULT_FILENAME,
    SCRIPT_INFO_FILENAME,
    TASK_INFO_FILENAME,
    TASKS_DIR,
    TASK_KIND_CONFIG,
    TASK_KIND_SHELL,
)
from pyruns.core.system_metrics import SystemMonitor
from pyruns.core.task_generator import TaskGenerator
from pyruns.core.gpu_scheduler import GpuSchedulerConfig
from pyruns.core.task_manager import (
    TaskManager,
    TaskStateConflict,
    active_task_run_index,
)
from pyruns.core.report import build_export_csv, load_export_metadata, run_export_rows
from pyruns.launcher import (
    bootstrap_shell_workspace,
    bootstrap_workspace,
    choose_config_file,
    choose_directory,
    choose_shell_file,
    choose_script_file,
    get_config_selection_metadata,
    list_config_candidates,
    list_script_candidates,
    list_workspace_candidates,
    native_picker_available,
    normalize_path,
    shell_project_root_for_workspace,
)
from pyruns.utils import get_now_str
from pyruns.utils.env_utils import is_valid_environment_name, normalize_environment
from pyruns.utils.batch_utils import count_batch_configs, generate_batch_configs, preview_batch_configs
from pyruns.utils.config_utils import (
    dump_config_text,
    load_config_text,
    list_template_files,
    preview_config_line,
    template_task_entry,
    to_container,
    validate_config_types_against_template,
)
from pyruns.utils.info_io import (
    get_log_options,
    load_script_info,
    load_task_metadata,
    resolve_log_path,
    run_history_status,
    task_attempt_matches,
    validate_task_log_path,
    validate_task_name,
    validate_tasks_root,
    validate_workspace_directory,
)
from pyruns.utils.log_io import log_file_identity, read_last_bytes, read_log_preview, safe_read_log
from pyruns.utils.file_io import map_disk_reads, read_bounded_bytes, regular_file_opener
from pyruns.utils.log_search import LogSearch, LogSearchResult
from pyruns.utils.process_utils import hidden_subprocess_kwargs
from pyruns.utils.settings import ensure_settings_file, load_settings, save_settings_for_root
from pyruns.utils.shell_runtime import get_shell_runtime_for_workspace
from pyruns.utils.sort_utils import filter_tasks, sort_tasks_for_manager
from pyruns.utils.runner_device import local_runner_host, runner_location
from pyruns.utils.task_storage import device_folder_host, new_task_root, task_source, task_storage_scope, visible_task_roots
from pyruns.utils.search_query import DEFAULT_MAX_SEARCH_RESULTS, SearchBudget, SearchQuery
from pyruns.utils.task_files import (
    MAX_TASK_PAYLOAD_BYTES,
    build_task_preview_and_search,
    build_task_search_result,
    task_search_found,
    normalize_task_kind,
    normalize_workspace_kind,
    read_task_payload_text,
    resolve_task_payload_path,
)


_DEFAULT_GPU_SCHEDULER_CONFIG = GpuSchedulerConfig.from_settings({})
MAX_MONITOR_LOG_TAIL_BYTES = _cfg.MAX_MONITOR_CHUNK_SIZE
EXPORT_LOG_TAIL_BYTES = 64 * 1024
MAX_JSON_EXPORT_BYTES = 32 * 1024 * 1024
# Larger batches reduce ripgrep startup costs for log-only requests.
# Streaming searches use smaller batches for early results. Match limits are
# consumed in task/field/file order independently of these execution batches.
_LOG_SEARCH_TASK_BATCH_SIZE = 128
_STREAM_LOG_SEARCH_TASK_BATCH_SIZE = 32
TaskManagerFactory = Callable[[str], TaskManager]
TaskGeneratorFactory = Callable[[str], TaskGenerator]
MetricsFactory = Callable[[], SystemMonitor]
SHELL_TEMPLATE_EXTENSIONS = {".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd"}
_GPU_SCHEDULER_PAYLOAD_KEYS = {
    "enabled": "gpu_scheduler_enabled",
    "task_mode": "gpu_scheduler_task_mode",
    "selection_mode": "gpu_scheduler_selection_mode",
    "gpus_per_task": "gpu_scheduler_gpus_per_task",
    "device_ids": "gpu_scheduler_device_ids",
    "min_free_memory_gb": "gpu_scheduler_min_free_memory_gb",
    "min_idle_pct": "gpu_scheduler_min_idle_pct",
    "stable_seconds": "gpu_scheduler_stable_seconds",
    "max_wait_seconds": "gpu_scheduler_max_wait_seconds",
    "respect_cuda_visible_devices": "gpu_scheduler_respect_cuda_visible_devices",
    "require_same_gpu_model": "gpu_scheduler_require_same_gpu_model",
}


def _comparison_metric_rows(left: Any, right: Any) -> List[Dict[str, Any]]:
    """Format metrics before JSON transport can round large integers in browsers."""
    def fields(value: Any) -> Dict[str, Any]:
        if value is None:
            return {}
        return value if isinstance(value, dict) else {"Record": value}

    missing = object()

    def display(value: Any) -> str:
        if value is missing:
            return "Not recorded"
        return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)

    before, after = fields(left), fields(right)
    rows = []
    for key in sorted(before.keys() | after.keys()):
        a, b = before.get(key, missing), after.get(key, missing)
        left_text, right_text = display(a), display(b)
        delta = None
        if type(a) is int and type(b) is int:
            delta = str(b - a)
        elif type(a) in (int, float) and type(b) in (int, float):
            # Mixing a float with an unsafe integer loses precision or overflows.
            if not any(type(value) is int and abs(value) > 2**53 - 1 for value in (a, b)):
                difference = b - a
                if math.isfinite(difference):
                    delta = format(difference, ".8g")
        rows.append({
            "key": key, "left": left_text, "right": right_text,
            "changed": left_text != right_text, "delta": delta,
        })
    return rows


class WorkspaceChangedError(RuntimeError):
    """Raised when a request or stream outlives its source workspace."""


class LogFileChangedError(ValueError):
    """The selected log was replaced before its download could be opened."""


@dataclass
class OpenedTaskLog:
    handle: BinaryIO
    filename: str
    size: int
    identity: str


expected_workspace: ContextVar[str | None] = ContextVar("expected_workspace", default=None)


class TaskNotesConflictError(RuntimeError):
    """Raised when task notes changed after an editor loaded them."""


class TaskEnvConflictError(RuntimeError):
    """Raised when task environment changed after an editor loaded it."""


def _with_stable_workspace(method: Callable[..., Any]) -> Callable[..., Any]:
    """Keep one workspace active for the full runtime operation."""

    @wraps(method)
    def wrapped(self: "PyrunsRuntime", *args: Any, **kwargs: Any) -> Any:
        with self._workspace_lock:
            self._check_expected_workspace()
            # Nested calls are part of this same atomic operation, including a
            # successful workspace switch which returns the new workspace.
            token = expected_workspace.set(None)
            try:
                return method(self, *args, **kwargs)
            finally:
                expected_workspace.reset(token)

    return wrapped


def _strip_unquoted_comment(value: str) -> str:
    """Strip shell-style comments that begin after whitespace outside quotes."""

    in_single = False
    in_double = False
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and not in_single:
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if char == "#" and not in_single and not in_double and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value


def _looks_like_windows_path_value(value: str) -> bool:
    normalized = str(value or "")
    return (
        len(normalized) >= 3
        and normalized[1] == ":"
        and normalized[0].isalpha()
        and normalized[2] in {"\\", "/"}
    ) or normalized.startswith("\\\\")


def parse_global_env_text(text: str) -> Dict[str, str]:
    """Parse workspace env text using a safe shell-assignment subset."""

    result: Dict[str, str] = {}
    for line_no, raw_line in enumerate(str(text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            raise ValueError(f"Line {line_no}: expected KEY=value")

        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not is_valid_environment_name(key):
            raise ValueError(f"Line {line_no}: invalid env name '{key}'")

        value_text = _strip_unquoted_comment(raw_value.strip())
        if not value_text:
            result[key] = ""
            continue

        if _looks_like_windows_path_value(value_text):
            result[key] = value_text
            continue

        lexer = shlex.shlex(value_text, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        try:
            parts = list(lexer)
        except ValueError as exc:
            raise ValueError(f"Line {line_no}: {exc}") from exc
        if len(parts) > 1:
            raise ValueError(f"Line {line_no}: quote values that contain spaces")
        result[key] = parts[0] if parts else ""
    return normalize_environment(result)


def _int_setting(settings: Dict[str, Any], key: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(settings.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _coerce_bool_payload(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _coerce_int_payload(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        numeric = float(str(value).strip())
        if not math.isfinite(numeric):
            return max(minimum, default)
        parsed = int(numeric)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, parsed)


def _coerce_float_payload(value: Any, default: float, *, minimum: float = 0.0) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return max(minimum, parsed)


def _coerce_gpu_device_ids_payload(value: Any) -> List[int]:
    if value in (None, "", "auto"):
        return []
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        return []
    ids: List[int] = []
    seen: set[int] = set()
    for raw in raw_items:
        text = str(raw).strip()
        if text.isdigit():
            value = int(text)
            if value not in seen:
                ids.append(value)
                seen.add(value)
    return ids


def _clean_gpu_scheduler_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(payload)
    if "min_idle_pct" not in payload and "compute_used_pct" in payload:
        payload["min_idle_pct"] = 100.0 - min(100.0, _coerce_float_payload(payload["compute_used_pct"], 30.0))
    clean: Dict[str, Any] = {}
    for key, setting_key in _GPU_SCHEDULER_PAYLOAD_KEYS.items():
        if key not in payload:
            continue
        value = payload[key]
        if key in {"enabled", "respect_cuda_visible_devices", "require_same_gpu_model"}:
            clean[setting_key] = _coerce_bool_payload(value)
        elif key == "task_mode":
            mode = str(value or "single").strip().lower()
            clean[setting_key] = "multi" if mode == "multi" else "single"
        elif key == "selection_mode":
            mode = str(value or "auto").strip().lower()
            clean[setting_key] = "specified" if mode in {"specified", "fixed", "manual"} else "auto"
        elif key == "device_ids":
            clean[setting_key] = _coerce_gpu_device_ids_payload(value)
        elif key == "gpus_per_task":
            clean[setting_key] = _coerce_int_payload(value, 1, minimum=1)
        elif key == "max_wait_seconds":
            clean[setting_key] = _coerce_float_payload(
                value,
                _DEFAULT_GPU_SCHEDULER_CONFIG.max_wait_seconds,
                minimum=0.0,
            )
        elif key == "min_idle_pct":
            default = 100.0 - _DEFAULT_GPU_SCHEDULER_CONFIG.compute_used_pct
            clean[setting_key] = min(100.0, _coerce_float_payload(value, default, minimum=0.0))
        elif key == "min_free_memory_gb":
            clean[setting_key] = _coerce_float_payload(
                value,
                _DEFAULT_GPU_SCHEDULER_CONFIG.min_free_memory_gb,
                minimum=0.0,
            )
        elif key == "stable_seconds":
            clean[setting_key] = _coerce_float_payload(
                value,
                _DEFAULT_GPU_SCHEDULER_CONFIG.stable_seconds,
                minimum=0.0,
            )
        else:
            clean[setting_key] = _coerce_float_payload(value, 0.0, minimum=0.0)
    return clean


def _clip_text_middle(text: str, max_chars: int) -> str:
    """Keep a bounded search hint while preserving both ends of the text."""

    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    marker = "\n...[truncated]...\n"
    if max_chars <= len(marker):
        return text[:max_chars]
    head_len = max_chars // 2
    tail_len = max_chars - head_len - len(marker)
    return f"{text[:head_len]}{marker}{text[-tail_len:]}"


def _require_bounded_editor_text(value: str, *, label: str) -> str:
    text = str(value or "")
    if len(text.encode("utf-8")) > MAX_TASK_PAYLOAD_BYTES:
        raise ValueError(f"{label} is too large (max {MAX_TASK_PAYLOAD_BYTES} bytes)")
    return text


def _cap_summary_task_payloads(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    max_chars = max(0, int(_cfg.DEFAULT_TASK_SUMMARY_SEARCH_TEXT_CHARS))
    capped: List[Dict[str, Any]] = []
    for task in tasks:
        search_text = str(task.get("search_text", "") or "")
        if len(search_text) <= max_chars:
            capped.append(task)
            continue
        item = dict(task)
        item["search_text"] = _clip_text_middle(search_text, max_chars)
        capped.append(item)
    return capped


@dataclass
class TaskPage:
    """Paginated task list result for the API layer."""

    items: List[Dict[str, Any]]
    total: int
    offset: int
    limit: int
    has_more: bool
    status_counts: Dict[str, int]
    search_errors: List[str] = field(default_factory=list)
    search_limit_hit: bool = False
    search_complete: bool = True
    task_sources: List[Dict[str, str]] = field(default_factory=list)


class PyrunsRuntime:
    """Owns the current workspace and lazily-instantiated service singletons."""

    def __init__(
        self,
        root_dir: str | None = None,
        *,
        task_manager_factory: TaskManagerFactory | None = None,
        task_generator_factory: TaskGeneratorFactory | None = None,
        metrics_factory: MetricsFactory | None = None,
    ) -> None:
        self.detached_task_runs = False
        self._task_manager_factory = (
            task_manager_factory
            if task_manager_factory is not None
            else lambda tasks_dir: TaskManager(
                tasks_dir=tasks_dir,
                lazy_scan=None,
                include_other_devices=True,
                owns_task_lifecycle=not self.detached_task_runs,
                watch_task_changes=self.detached_task_runs,
                reconcile_unowned_running=True,
            )
        )
        self._task_generator_factory = (
            task_generator_factory
            if task_generator_factory is not None
            else lambda tasks_dir: TaskGenerator(root_dir=tasks_dir)
        )
        self._metrics_factory = metrics_factory if metrics_factory is not None else SystemMonitor

        self._workspace_lock = threading.RLock()
        self._workspace_epoch = 0
        self._lock = threading.RLock()
        self.root_dir = ""
        self.tasks_dir = ""
        self.settings: Dict[str, Any] = {}
        self._task_manager: TaskManager | None = None
        self._task_managers: Dict[str, TaskManager] = {}
        self._task_manager_callbacks: Dict[str, Callable[[], None]] = {}
        self._task_generator: TaskGenerator | None = None
        self._metrics_sampler: SystemMonitor | None = None
        self._tasks_loaded = False
        self._log_search = LogSearch()
        self._conda_envs_cache: Dict[str, Any] | None = None
        self._template_scan: tuple[int, Future[List[Dict[str, str]]]] | None = None
        self.reload(root_dir)

    def _check_expected_workspace(self, workspace_root: str | None = None) -> None:
        """Check the client binding while the caller holds ``_workspace_lock``."""
        requested = expected_workspace.get() if workspace_root is None else workspace_root
        if requested is not None and os.path.normcase(os.path.abspath(requested)) != os.path.normcase(os.path.abspath(self.root_dir)):
            raise WorkspaceChangedError("Workspace changed; reload the workspace before retrying.")

    @staticmethod
    def _normalize_path(path: str) -> str:
        return normalize_path(path)

    @staticmethod
    def _has_workspace_markers(path: str) -> bool:
        tasks_path = os.path.join(path, TASKS_DIR)
        has_tasks = os.path.isdir(tasks_path)
        has_config = os.path.exists(os.path.join(path, CONFIG_DEFAULT_FILENAME))
        has_script = os.path.exists(os.path.join(path, SCRIPT_INFO_FILENAME))
        return has_tasks or has_config or has_script

    @_with_stable_workspace
    def reload(self, root_dir: str | None = None) -> None:
        """Activate a workspace without interrupting tasks owned in other workspaces."""
        resolved_root = self._normalize_path(
            root_dir or os.getenv(_cfg.ENV_KEY_ROOT, _cfg.ROOT_DIR)
        )
        tasks_dir = self._normalize_path(os.path.join(resolved_root, TASKS_DIR))

        validate_workspace_directory(resolved_root)
        validate_tasks_root(tasks_dir)
        _cfg.ROOT_DIR = resolved_root
        os.environ[_cfg.ENV_KEY_ROOT] = resolved_root
        os.makedirs(tasks_dir, exist_ok=True)
        validate_workspace_directory(resolved_root)
        validate_tasks_root(tasks_dir)
        ensure_settings_file(resolved_root)

        manager_key = os.path.normcase(os.path.abspath(tasks_dir))
        with self._lock:
            previous_manager = self._task_manager
            self._workspace_epoch += 1
            self.root_dir = resolved_root
            self.tasks_dir = tasks_dir
            self.settings = load_settings(resolved_root)
            self._task_manager = self._task_managers.get(manager_key)
            self._task_generator = None
            self._metrics_sampler = None
            self._tasks_loaded = False
            self._conda_envs_cache = None

            cached_managers = list(self._task_managers.items())

        for cached_key, cached_manager in cached_managers:
            if cached_key != manager_key:
                self._retire_task_manager_if_idle(cached_key, cached_manager)

        if (previous_manager is not None and previous_manager is not self._task_manager
                and hasattr(previous_manager, "trigger_update")):
            # Reuse the manager's observers so old event streams wake immediately.
            # Dispatch after releasing _lock; this does not scan task files.
            previous_manager.trigger_update()

    @staticmethod
    def _task_manager_is_active(task_manager: TaskManager) -> bool:
        """Conservatively report whether a manager still owns queued or running work."""

        if bool(getattr(task_manager, "is_processing", False)):
            return True
        if hasattr(task_manager, "has_reactive_watchers") and task_manager.has_reactive_watchers():
            return True
        tasks = list(getattr(task_manager, "tasks", []) or [])
        return any(
            str((task or {}).get("status", "") or "").lower() in {"queued", "running"}
            and task_storage_scope((task or {}).get("dir", "")) != "remote"
            for task in tasks
        )

    def _managed_task_managers(self) -> list[TaskManager]:
        with self._lock:
            managers = list(dict.fromkeys(self._task_managers.values()))
            if self._task_manager is not None and self._task_manager not in managers:
                managers.append(self._task_manager)
            return managers

    def _active_task_count(self, *, force_all: bool, restart_only: bool = False) -> int:
        self.ensure_tasks_loaded(full_refresh=False)
        active_count = 0
        for manager in self._managed_task_managers():
            if force_all:
                active_count += self._strict_manager_active_task_count(manager, restart_only=restart_only)
            else:
                manager.refresh_from_disk(check_all=True, discover=True, check_payload=False)
                active_count += manager.active_task_count(for_operation="upgrade")
        return active_count

    @staticmethod
    def _strict_manager_active_task_count(manager: TaskManager, *, restart_only: bool = False) -> int:
        """Retry changing snapshots, but never interpret an unreadable workspace as idle."""
        operation = "restart" if restart_only else "upgrade"
        attempts = 0
        while True:
            attempts += 1
            try:
                manager.refresh_from_disk(force_all=True, discover=True, raise_on_error=True)
                return manager.active_task_count(for_operation=operation)
            except Exception as exc:
                if isinstance(exc, TaskStateConflict):
                    # Heartbeats can prevent a stable read of an active task.
                    # A positive cached count safely refuses an update; zero
                    # must still be verified by a successful strict refresh.
                    active_count = manager.active_task_count(for_operation=operation)
                    if active_count:
                        return active_count
                    if attempts < 3:
                        continue
                raise RuntimeError(
                    f"Workspace '{os.path.dirname(manager.tasks_dir)}': {type(exc).__name__}: {exc}"
                ) from exc

    @_with_stable_workspace
    def active_task_count(self) -> int:
        """Count active work without probing unchanged tasks' payloads."""

        return self._active_task_count(force_all=False)

    @_with_stable_workspace
    def strict_active_task_count(self) -> int:
        """Re-read every task before an operation that requires a proven idle UI."""

        return self._active_task_count(force_all=True)

    @_with_stable_workspace
    def strict_restart_active_task_count(self) -> int:
        """Verify the work that must finish before this UI process exits."""
        if self.detached_task_runs:
            return 0
        return self._active_task_count(force_all=True, restart_only=True)

    def cached_restart_active_task_count(self) -> int:
        """Use the refreshed registry for the shared restart heartbeat."""
        if self.detached_task_runs:
            return 0
        return sum(manager.active_task_count(for_operation="restart") for manager in self._managed_task_managers())

    def _retire_task_manager_if_idle(self, manager_key: str, task_manager: TaskManager) -> None:
        """Stop and forget one non-current manager after its active work has finished."""

        callback: Callable[[], None] | None = None
        with self._lock:
            if self._task_manager is task_manager:
                return
            if self._task_managers.get(manager_key) is not task_manager:
                return
            if self._task_manager_is_active(task_manager):
                return
            self._task_managers.pop(manager_key, None)
            callback = self._task_manager_callbacks.pop(manager_key, None)

        if callback is not None and hasattr(task_manager, "off_change"):
            task_manager.off_change(callback)
        task_manager.shutdown()

    def _track_task_manager(self, manager_key: str, task_manager: TaskManager) -> None:
        """Cache one manager and observe it so inactive background managers are reclaimed."""

        self._task_managers[manager_key] = task_manager
        if not hasattr(task_manager, "on_change"):
            return

        def callback() -> None:
            self._retire_task_manager_if_idle(manager_key, task_manager)

        self._task_manager_callbacks[manager_key] = callback
        task_manager.on_change(callback)

    def shutdown(self) -> None:
        """Release background services owned by this runtime."""
        with self._lock:
            tracked_managers = list(self._task_managers.items())
            task_managers = list(dict.fromkeys(manager for _, manager in tracked_managers))
            if self._task_manager is not None and self._task_manager not in task_managers:
                task_managers.append(self._task_manager)
            tracked_callbacks = [
                (manager, self._task_manager_callbacks.get(manager_key))
                for manager_key, manager in tracked_managers
            ]
            self._task_manager = None
            self._task_managers.clear()
            self._task_manager_callbacks.clear()
            self._task_generator = None
            self._metrics_sampler = None
            self._tasks_loaded = False

        for task_manager, callback in tracked_callbacks:
            if callback is not None and hasattr(task_manager, "off_change"):
                task_manager.off_change(callback)
        for task_manager in task_managers:
            task_manager.shutdown()

    @_with_stable_workspace
    def change_run_root(self, new_root: str, *, include_templates: bool = True) -> Dict[str, Any]:
        """Switch the active workspace after validation."""
        resolved_root = self._normalize_path(new_root)
        if not self._has_workspace_markers(resolved_root):
            raise ValueError(
                f"Run Root must contain {TASKS_DIR}/, {CONFIG_DEFAULT_FILENAME}, or {SCRIPT_INFO_FILENAME}"
            )
        self.reload(resolved_root)
        return self.get_workspace_info(include_templates=include_templates)

    @_with_stable_workspace
    def get_workspace_info(self, *, include_templates: bool = True) -> Dict[str, Any]:
        """Return current workspace metadata for the frontend."""
        script_info = load_script_info(self.root_dir)
        script_path = str(script_info.get("script_path", "") or "")
        project_root = str(script_info.get("project_root", "") or "")
        raw_workspace_kind = str(script_info.get("workspace_kind", "") or "").strip().lower()
        workspace_kind = normalize_workspace_kind(raw_workspace_kind)
        workspace_ready = bool(
            script_path
            or script_info.get("script_name")
            or raw_workspace_kind == _cfg.WORKSPACE_KIND_SHELL
            or (raw_workspace_kind == _cfg.WORKSPACE_KIND_SCRIPT and project_root)
        )
        if workspace_kind == _cfg.WORKSPACE_KIND_SHELL and not project_root:
            project_root = shell_project_root_for_workspace(self.root_dir)
        if workspace_kind == _cfg.WORKSPACE_KIND_SHELL:
            working_root = project_root
        elif script_path:
            working_root = self._normalize_path(os.path.dirname(script_path))
        else:
            working_root = ""
        default_template = None
        if workspace_kind != _cfg.WORKSPACE_KIND_SHELL:
            default_path = os.path.join(self.root_dir, CONFIG_DEFAULT_FILENAME).replace("\\", "/")
            if os.path.isfile(default_path):
                default_template = {"value": default_path, "label": self._default_template_label(script_info)}
        return {
            "run_root": self.root_dir,
            "working_root": working_root,
            "native_file_picker": native_picker_available(),
            "device_host": local_runner_host(),
            "tasks_dir": self.tasks_dir,
            "script_path": script_path,
            "script_name": str(script_info.get("script_name", "") or ""),
            "config_default_source": str(script_info.get("config_default_source", "") or ""),
            "config_default_source_name": str(script_info.get("config_default_source_name", "") or ""),
            "default_template": default_template,
            "project_root": project_root,
            "workspace_kind": workspace_kind,
            "workspace_ready": workspace_ready,
            "settings": dict(self.settings),
            "shell_runtime": get_shell_runtime_for_workspace(self.root_dir),
            "templates": self.list_templates() if include_templates else [],
        }

    @staticmethod
    def _clean_setting_text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _resolve_executable(candidate: str) -> str:
        raw = os.path.expanduser(os.path.expandvars(str(candidate or "").strip()))
        if not raw:
            return ""
        if os.path.isabs(raw) or os.path.dirname(raw):
            return os.path.abspath(raw) if os.path.exists(raw) else ""
        resolved = shutil.which(raw)
        return os.path.abspath(resolved) if resolved else ""

    @staticmethod
    def _env_name_from_path(path: str, root_prefix: str = "") -> str:
        normalized = os.path.normpath(str(path or ""))
        if root_prefix and os.path.normcase(os.path.abspath(normalized)) == os.path.normcase(os.path.abspath(root_prefix)):
            return "base"
        leaf = os.path.basename(normalized)
        return leaf or normalized

    def _conda_executable_for_runtime(self, settings: Dict[str, Any] | None = None) -> str:
        source = self.settings if settings is None else settings
        configured = self._clean_setting_text(source.get("conda_executable"))
        if configured and configured != "conda":
            return configured
        return self._clean_setting_text(os.getenv("CONDA_EXE")) or configured or "conda"

    def list_conda_envs(self, *, refresh: bool = True) -> Dict[str, Any]:
        """Return conda environments discoverable from the current server process."""

        with self._workspace_lock:
            self._check_expected_workspace()
            workspace_epoch = self._workspace_epoch
            settings = dict(self.settings)
            cached = dict(self._conda_envs_cache) if self._conda_envs_cache is not None else None
        return self._list_conda_envs_for_snapshot(
            settings,
            workspace_epoch=workspace_epoch,
            refresh=refresh,
            cached=cached,
        )

    def _cache_conda_envs_for_snapshot(
        self,
        workspace_epoch: int,
        payload: Dict[str, Any],
    ) -> None:
        with self._workspace_lock:
            if workspace_epoch == self._workspace_epoch:
                self._conda_envs_cache = dict(payload)

    def _list_conda_envs_for_snapshot(
        self,
        settings: Dict[str, Any],
        *,
        workspace_epoch: int,
        refresh: bool,
        cached: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        """Discover providers for one immutable workspace settings snapshot."""

        if not refresh and cached is not None:
            return dict(cached)
        if not refresh:
            return {
                "available": False,
                "executable": self._conda_executable_for_runtime(settings),
                "envs": [],
                "error": "Conda providers have not been refreshed yet.",
            }

        conda_executable = self._conda_executable_for_runtime(settings)
        resolved_conda = self._resolve_executable(conda_executable)
        if not resolved_conda:
            payload = {
                "available": False,
                "executable": conda_executable,
                "envs": [],
                "error": f"conda executable not found: {conda_executable}",
            }
            self._cache_conda_envs_for_snapshot(workspace_epoch, payload)
            return payload

        root_prefix = ""
        try:
            info_result = subprocess.run(
                [resolved_conda, "info", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                **hidden_subprocess_kwargs(),
            )
            if info_result.returncode == 0:
                info_data = yaml.safe_load(info_result.stdout) or {}
                if isinstance(info_data, dict):
                    root_prefix = str(info_data.get("root_prefix", "") or "")
        except Exception:
            root_prefix = ""

        try:
            result = subprocess.run(
                [resolved_conda, "env", "list", "--json"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                **hidden_subprocess_kwargs(),
            )
        except Exception as exc:
            payload = {
                "available": False,
                "executable": resolved_conda,
                "envs": [],
                "error": str(exc),
            }
            self._cache_conda_envs_for_snapshot(workspace_epoch, payload)
            return payload

        if result.returncode != 0:
            payload = {
                "available": False,
                "executable": resolved_conda,
                "envs": [],
                "error": result.stderr.strip() or result.stdout.strip() or "conda env list failed",
            }
            self._cache_conda_envs_for_snapshot(workspace_epoch, payload)
            return payload

        data = yaml.safe_load(result.stdout) or {}
        raw_envs = data.get("envs", []) if isinstance(data, dict) else []
        envs = []
        seen_names: set[str] = set()
        for raw_path in raw_envs if isinstance(raw_envs, list) else []:
            path = self._normalize_path(str(raw_path))
            name = self._env_name_from_path(path, root_prefix)
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            envs.append({
                "name": name,
                "path": path,
                "python_executable": self._normalize_path(os.path.join(path, "python.exe" if os.name == "nt" else "bin/python")),
                "active": name == os.getenv("CONDA_DEFAULT_ENV") or path == os.getenv("CONDA_PREFIX"),
            })

        payload = {
            "available": True,
            "executable": resolved_conda,
            "envs": envs,
            "error": "",
        }
        self._cache_conda_envs_for_snapshot(workspace_epoch, payload)
        return payload

    def get_runtime_info(self, *, refresh_providers: bool = True) -> Dict[str, Any]:
        """Return runtime settings and environment providers for the current workspace."""

        with self._workspace_lock:
            self._check_expected_workspace()
            workspace_epoch = self._workspace_epoch
            settings = dict(self.settings)
            cached_conda = (
                dict(self._conda_envs_cache)
                if self._conda_envs_cache is not None
                else None
            )

        provider_reader = self.list_conda_envs
        if getattr(provider_reader, "__func__", None) is PyrunsRuntime.list_conda_envs:
            conda = self._list_conda_envs_for_snapshot(
                settings,
                workspace_epoch=workspace_epoch,
                refresh=refresh_providers,
                cached=cached_conda,
            )
        else:
            # Preserve deliberate test/subclass provider overrides.
            conda = provider_reader(refresh=refresh_providers)
        global_env = settings.get("global_env", {})
        if not isinstance(global_env, dict):
            global_env = {}
        gpu_config = GpuSchedulerConfig.from_settings(settings)
        runtime = {
            "task_storage": settings.get("task_storage", "shared"),
            "python_executable": self._clean_setting_text(settings.get("python_executable")),
            "conda_env": self._clean_setting_text(settings.get("conda_env")),
            "conda_executable": self._clean_setting_text(settings.get("conda_executable")) or "conda",
            "global_env": {str(k): str(v) for k, v in global_env.items()},
            "gpu_scheduler": {
                "enabled": gpu_config.enabled,
                "task_mode": "multi" if gpu_config.task_mode == "multi" else "single",
                "selection_mode": "specified" if gpu_config.uses_specified_devices else "auto",
                "gpus_per_task": gpu_config.required_gpu_count,
                "device_ids": list(gpu_config.device_ids),
                "min_free_memory_gb": gpu_config.min_free_memory_gb,
                "min_idle_pct": 100.0 - gpu_config.compute_used_pct,
                "stable_seconds": gpu_config.stable_seconds,
                "max_wait_seconds": gpu_config.max_wait_seconds,
                "respect_cuda_visible_devices": gpu_config.respect_cuda_visible_devices,
                "require_same_gpu_model": gpu_config.require_same_gpu_model,
            },
            "process": {
                "python_executable": os.path.abspath(sys.executable),
                "conda_env": os.getenv("CONDA_DEFAULT_ENV", ""),
                "conda_prefix": os.getenv("CONDA_PREFIX", ""),
            },
            "providers": [
                {"id": "conda", "label": "Conda", "available": bool(conda.get("available"))},
            ],
            "conda": conda,
        }
        return runtime

    @_with_stable_workspace
    def update_runtime_settings(self, payload: Dict[str, Any], *, refresh_providers: bool = False) -> Dict[str, Any]:
        """Persist runtime settings and return the refreshed runtime info."""

        allowed = {"python_executable", "conda_env", "conda_executable", "global_env", "global_env_text", "gpu_scheduler", "task_storage"}
        current_conda_executable = self._clean_setting_text(self.settings.get("conda_executable")) or "conda"
        provider_settings_changed = False
        updates: Dict[str, Any] = {}
        if "global_env" in payload and "global_env_text" in payload:
            raise ValueError("global_env and global_env_text cannot be updated together")
        for key, value in payload.items():
            if key not in allowed:
                continue
            if key == "conda_executable":
                provider_settings_changed = (self._clean_setting_text(value) or "conda") != current_conda_executable
            if key == "task_storage" and value not in ("shared", "local"):
                raise ValueError("task_storage must be shared or local")
            if key == "gpu_scheduler":
                if not isinstance(value, dict):
                    raise ValueError("gpu_scheduler must be an object")
                updates.update(_clean_gpu_scheduler_payload(value))
            elif key == "global_env_text":
                updates["global_env"] = parse_global_env_text(str(value or ""))
            elif key == "global_env":
                clean_env = normalize_environment(value, drop_none_values=True)
                updates[key] = clean_env
            else:
                updates[key] = self._clean_setting_text(value)

        save_settings_for_root(self.root_dir, updates)

        self.settings = load_settings(self.root_dir)
        if "task_storage" in updates:
            with self._lock:
                self._task_generator = None
        if provider_settings_changed:
            self._conda_envs_cache = None
        return self.get_runtime_info(refresh_providers=refresh_providers)

    def list_templates(self) -> List[Dict[str, str]]:
        """Share a catalog read without holding the workspace lock during disk I/O."""
        with self._workspace_lock:
            self._check_expected_workspace()
            root_dir, tasks_dir, epoch = self.root_dir, self.tasks_dir, self._workspace_epoch
            pending = self._template_scan
            if pending is None or pending[0] != epoch:
                pending = (epoch, Future())
                self._template_scan = pending
                owns_scan = True
            else:
                owns_scan = False
        future = pending[1]
        if owns_scan:
            try:
                future.set_result(self._read_template_options(root_dir, tasks_dir))
            except BaseException as exc:
                future.set_exception(exc)
            finally:
                with self._workspace_lock:
                    if self._template_scan is pending:
                        self._template_scan = None
        result = future.result()
        with self._workspace_lock:
            if epoch != self._workspace_epoch:
                raise WorkspaceChangedError("Workspace changed; reload the workspace before retrying.")
        return result

    @staticmethod
    def _default_template_label(script_info: Dict[str, Any]) -> str:
        source_name = str(script_info.get("config_default_source_name", "") or "")
        if source_name:
            return f"{CONFIG_DEFAULT_FILENAME} (from {source_name})"
        return CONFIG_DEFAULT_FILENAME

    def _read_template_options(self, root_dir: str, tasks_dir: str) -> List[Dict[str, str]]:
        script_info = load_script_info(root_dir)
        if normalize_workspace_kind(script_info.get("workspace_kind")) == _cfg.WORKSPACE_KIND_SHELL:
            return self._list_shell_templates(tasks_dir)
        return [
            {"value": value, "label": self._default_template_label(script_info) if os.path.isabs(value) else label}
            for value, label in list_template_files(root_dir).items()
        ]

    @_with_stable_workspace
    def list_shell_templates(self, script_info: Dict[str, Any] | None = None) -> List[Dict[str, str]]:
        """Return existing shell task payloads that can seed shell-mode tasks."""
        return self._list_shell_templates(self.tasks_dir)

    @staticmethod
    def _shell_template_payload(task_dir: str) -> tuple[str, Dict[str, Any]]:
        resolved_paths: dict[str, str | None] = {}
        info = load_task_metadata(task_dir, _resolved_paths=resolved_paths)
        if normalize_task_kind(info.get("task_kind")) != _cfg.TASK_KIND_SHELL:
            return "", info
        configured_file = str(info.get("config_file", "") or "")
        for name in dict.fromkeys((configured_file, *_cfg.SHELL_CONFIG_FILENAMES)):
            if not name:
                continue
            try:
                payload = resolve_task_payload_path(task_dir, name, _resolved_paths=resolved_paths)
            except ValueError:
                continue
            if os.path.isfile(payload):
                return payload, info
        return "", info

    def _list_shell_templates(self, tasks_root: str) -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []

        def load_entry(task_dir: str) -> Dict[str, Any] | None:
            payload, info = self._shell_template_payload(task_dir)
            return template_task_entry(task_dir, payload, info) if payload else None

        for tasks_dir in visible_task_roots(tasks_root):
            validate_tasks_root(tasks_dir)
            if not os.path.isdir(tasks_dir):
                continue
            with os.scandir(tasks_dir) as entries:
                folders = sorted(entry.path for entry in entries
                                 if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False))
            task_entries = [entry for entry in map_disk_reads(folders, load_entry) if entry is not None]
            for task in sort_tasks_for_manager(task_entries):
                items.append({"value": str(task["_template_path"]), "label": str(task["name"])})

        return items

    def _fallback_template_label(self, path: str, script_info: Dict[str, Any]) -> str:
        """Return a readable label for templates loaded outside the picker list."""

        workspace_kind = normalize_workspace_kind(script_info.get("workspace_kind"))
        if workspace_kind == _cfg.WORKSPACE_KIND_SHELL:
            project_root = shell_project_root_for_workspace(self.root_dir)
            project_root = self._normalize_path(project_root)
            try:
                common_root = self._normalize_path(os.path.commonpath([path, project_root]))
                if project_root and os.path.normcase(common_root) == os.path.normcase(project_root):
                    return os.path.relpath(path, project_root).replace("\\", "/")
            except (OSError, ValueError):
                pass

        return os.path.basename(path)

    @_with_stable_workspace
    def resolve_template_path(self, template_value: str) -> str:
        """Resolve one template without allowing reads outside workspace-owned roots."""

        value = str(template_value or "").strip()
        if not value:
            raise FileNotFoundError("Template value is empty.")

        if os.path.isabs(value):
            path = value
        else:
            path = os.path.join(self.root_dir, value)

        resolved = os.path.realpath(self._normalize_path(path))
        if not os.path.isfile(resolved):
            raise FileNotFoundError(f"Template not found: {template_value}")

        allowed_roots = [os.path.realpath(self._normalize_path(self.root_dir))]
        script_info = load_script_info(self.root_dir)
        if normalize_workspace_kind(script_info.get("workspace_kind")) == _cfg.WORKSPACE_KIND_SHELL:
            project_root = shell_project_root_for_workspace(self.root_dir)
            if project_root:
                allowed_roots.append(os.path.realpath(self._normalize_path(project_root)))

        contained = False
        for root in allowed_roots:
            try:
                if os.path.normcase(os.path.commonpath([resolved, root])) == os.path.normcase(root):
                    contained = True
                    break
            except (OSError, ValueError):
                continue
        if not contained:
            raise ValueError(f"Template resolves outside the allowed workspace: {template_value}")
        return self._normalize_path(resolved)

    @_with_stable_workspace
    def get_template_content(self, template_value: str) -> Dict[str, Any]:
        """Return the raw template text and metadata for one template."""

        path = self.resolve_template_path(template_value)
        content = self._read_template_text(path, template_value)

        script_info = load_script_info(self.root_dir)
        workspace_kind = normalize_workspace_kind(script_info.get("workspace_kind"))
        default_path = os.path.join(self.root_dir, CONFIG_DEFAULT_FILENAME)
        parts = str(template_value).split("/")
        if workspace_kind != _cfg.WORKSPACE_KIND_SHELL and os.path.normcase(path) == os.path.normcase(self._normalize_path(default_path)):
            label = self._default_template_label(script_info)
        elif len(parts) >= 3 and not parts[1].startswith(".") and parts[0] in {
            os.path.basename(root) for root in visible_task_roots(self.tasks_dir)
        }:
            task_dir = os.path.join(self.root_dir, parts[0], parts[1])
            if workspace_kind == _cfg.WORKSPACE_KIND_SHELL:
                payload, _ = self._shell_template_payload(task_dir)
            else:
                payload = os.path.join(task_dir, CONFIG_FILENAME)
            label = parts[1] if payload and os.path.normcase(self._normalize_path(payload)) == os.path.normcase(path) else (
                self._fallback_template_label(path, script_info)
            )
        else:
            label = self._fallback_template_label(path, script_info)
        suffix = os.path.splitext(path)[1].lower()
        mode_hint = "shell" if (
            workspace_kind == _cfg.WORKSPACE_KIND_SHELL
            or suffix in SHELL_TEMPLATE_EXTENSIONS
        ) else "yaml"
        data: Any = None
        if mode_hint == "yaml":
            try:
                data = load_config_text(content)
            except (ValueError, yaml.YAMLError):
                data = None
        returned_value = path if os.path.isabs(str(template_value or "")) else template_value

        return {
            "value": returned_value,
            "label": label,
            "path": path,
            "content": content,
            "read_only": os.path.basename(path) == CONFIG_DEFAULT_FILENAME,
            "mode_hint": mode_hint,
            "parsed_config": to_container(data, resolve=False) if isinstance(data, DictConfig) else None,
        }

    @staticmethod
    def _read_template_text(path: str, template_value: str) -> str:
        with open(path, "rb", opener=regular_file_opener) as handle:
            raw = read_bounded_bytes(handle, MAX_TASK_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_TASK_PAYLOAD_BYTES:
            raise ValueError(
                f"Template is too large (max {MAX_TASK_PAYLOAD_BYTES} bytes): {template_value}"
            )
        return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")

    def _load_generator_template_config(self, template_value: str) -> DictConfig:
        try:
            path = self.resolve_template_path(template_value)
            parsed = load_config_text(self._read_template_text(path, template_value))
            if not isinstance(parsed, DictConfig):
                raise ValueError(f"YAML root must be a mapping: {path}")
            return parsed
        except ValueError:
            raise
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"Could not load template '{template_value}': {exc}") from exc

    @property
    def task_generator(self) -> TaskGenerator:
        with self._lock:
            if self._task_generator is None:
                root = new_task_root(self.tasks_dir, self.settings.get("task_storage", "shared"))
                self._task_generator = self._task_generator_factory(root)
            return self._task_generator

    @property
    def task_manager(self) -> TaskManager:
        with self._lock:
            if self._task_manager is None:
                self._task_manager = self._task_manager_factory(self.tasks_dir)
                manager_key = os.path.normcase(os.path.abspath(self.tasks_dir))
                self._track_task_manager(manager_key, self._task_manager)
            return self._task_manager

    @property
    def metrics_sampler(self) -> SystemMonitor:
        with self._lock:
            if self._metrics_sampler is None:
                self._metrics_sampler = self._metrics_factory()
            return self._metrics_sampler

    def invalidate_cache(self, task_names: List[str] | None = None) -> None:
        """Invalidate the snapshot shared by UI requests and the scheduler."""
        with self._lock:
            manager = self._task_manager
        if manager is not None:
            manager.invalidate_ui_snapshot(task_names)

    @_with_stable_workspace
    def ensure_tasks_loaded(self, *, full_refresh: bool = False, force_refresh: bool = False) -> None:
        """Load task metadata on demand for faster startup."""
        manager = self.task_manager
        if not self._tasks_loaded or full_refresh or force_refresh:
            manager.refresh_ui_snapshot(force=force_refresh or not self._tasks_loaded)
            self._tasks_loaded = True

    @_with_stable_workspace
    def get_task_event_stream_context(self, *, workspace_root: str | None = None) -> tuple[str, TaskManager]:
        """Capture one atomic workspace/manager pair for a task event stream."""
        self._check_expected_workspace(workspace_root)
        self.ensure_tasks_loaded(full_refresh=False)
        manager = self.task_manager
        manager.acquire_reactive_watch()
        return os.path.normcase(os.path.abspath(self.root_dir)), manager

    @_with_stable_workspace
    def release_task_event_stream_context(
        self,
        expected_root: str,
        manager: TaskManager,
    ) -> None:
        """Release and promptly reclaim a task event stream's manager."""
        manager.release_reactive_watch()
        manager_key = os.path.normcase(os.path.abspath(manager.tasks_dir))
        if not self.workspace_stream_is_current(expected_root, manager):
            self._retire_task_manager_if_idle(manager_key, manager)

    @_with_stable_workspace
    def workspace_stream_is_current(
        self,
        expected_root: str,
        expected_manager: TaskManager | None = None,
    ) -> bool:
        """Return whether a long-lived stream still belongs to the active workspace."""
        current_root = os.path.normcase(os.path.abspath(self.root_dir))
        if current_root != os.path.normcase(os.path.abspath(expected_root)):
            return False
        return expected_manager is None or self._task_manager is expected_manager

    @_with_stable_workspace
    def get_task_log_stream_context(self, task_name: str, *, workspace_root: str | None = None) -> tuple[str, Dict[str, Any]]:
        """Capture one atomic workspace/task pair for a log stream."""
        self._check_expected_workspace(workspace_root)
        task = self.get_task(task_name, refresh=False, summary=True)
        if task is None:
            raise KeyError(task_name)
        return os.path.normcase(os.path.abspath(self.root_dir)), task

    @_with_stable_workspace
    def get_running_log_sources(self, task_names: list[str]) -> dict[str, tuple[str, int]]:
        manager = self._task_manager
        return manager.get_running_log_sources(task_names) if manager is not None else {}

    @_with_stable_workspace
    def list_tasks(
        self,
        *,
        query: str = "",
        status: str = "All",
        offset: int = 0,
        limit: int = 50,
        refresh: bool = True,
        force_refresh: bool = False,
        summary: bool = False,
        compact: bool = False,
        sort_mode: str = "priority",
        search_field: str = "all",
        sources: set[str] | None = None,
    ) -> TaskPage:
        """Return tasks in the same logical order as the Manager page."""
        self.ensure_tasks_loaded(full_refresh=refresh, force_refresh=force_refresh)
        safe_offset = max(0, int(offset))
        safe_limit = max(0, int(limit))
        items, total, status_counts = self.task_manager.get_task_page(
            query=query, status=status, offset=safe_offset, limit=safe_limit,
            sort_mode=sort_mode, search_field=search_field, summary=summary, compact=compact,
            sources=sources,
        )
        if summary:
            if query and items:
                results_by_name = self.task_manager.get_task_search_results(
                    [str(task.get("name", "") or "") for task in items],
                    query,
                    search_field=search_field,
                )
                enriched_items: List[Dict[str, Any]] = []
                for task in items:
                    result = results_by_name.get(
                        str(task.get("name", "") or ""),
                        {"matches": [], "match_count": 0},
                    )
                    enriched_items.append(
                        {
                            **task,
                            "search_matches": result["matches"],
                            "search_match_count": result["match_count"],
                        }
                    )
                items = enriched_items
            items = _cap_summary_task_payloads(items)
        has_more = (safe_offset + len(items)) < total
        return TaskPage(
            items=items,
            total=total,
            offset=safe_offset,
            limit=safe_limit,
            has_more=has_more,
            status_counts=status_counts,
            task_sources=self.task_manager.task_sources(),
        )

    @contextmanager
    def _search_workspace_lock(self, cancelled: threading.Event):
        """Let abandoned searches leave the queue before unrelated work ends."""
        if cancelled.is_set():
            raise CancelledError()
        while not self._workspace_lock.acquire(timeout=0.1):
            if cancelled.is_set():
                raise CancelledError()
        try:
            if cancelled.is_set():
                raise CancelledError()
            yield
        finally:
            self._workspace_lock.release()

    def search_tasks(self, *, query, status="All", offset=0, limit=50, sort_mode="priority", search_field="all",
                     match_case=False, whole_word=False, use_regex=False, include_logs=True, summary=True,
                     refresh=True, force_refresh=False, cancelled, max_results=DEFAULT_MAX_SEARCH_RESULTS,
                     on_progress=None, compact=False, sources=None):
        """Search metadata and all log files without holding task/workspace locks during I/O."""
        matcher = SearchQuery(query, match_case=match_case, whole_word=whole_word, use_regex=use_regex, cancelled=cancelled)
        while not self._log_search.slots.acquire(timeout=0.1):
            if cancelled.is_set():
                raise CancelledError()
        try:
            if cancelled.is_set():
                raise CancelledError()
            with self._search_workspace_lock(cancelled):
                self._check_expected_workspace()
                epoch = self._workspace_epoch
                self.ensure_tasks_loaded(full_refresh=refresh, force_refresh=force_refresh)
                manager = self.task_manager
                views = manager._get_task_search_views()
                task_sources = manager.task_sources()
            tasks = [task for task in views.values() if sources is None or task_source(task["dir"]) in sources]
            counts = dict.fromkeys(("pending", "queued", "running", "completed", "failed", "cancelled"), 0)
            for task in tasks:
                task_status = str(task.get("status") or "pending").lower()
                counts[task_status] = counts.get(task_status, 0) + 1
            needles = matcher.needles
            search_logs = bool(needles) and (search_field == "log" or (search_field == "all" and include_logs))
            candidates = filter_tasks(tasks, "", status)
            if not search_logs:
                # Only matching metadata needs sort keys and page selection.
                matched = []
                for task in candidates:
                    if cancelled.is_set():
                        raise CancelledError()
                    found = task_search_found(task, matcher, search_field)
                    if all(needle in found for needle in needles):
                        matched.append(task)
                candidates = matched
            ordered = sort_tasks_for_manager(candidates, sort_mode)
            total = 0
            items = []
            errors = []
            budget = SearchBudget(max_results if max_results and max_results > 0 else None)
            empty_logs: LogSearchResult = {"matches": [], "match_count": 0, "found": set(), "errors": []}

            def page(complete, batch_items=(), batch_total=0, batch_errors=()):
                with self._search_workspace_lock(cancelled):
                    if epoch != self._workspace_epoch:
                        raise WorkspaceChangedError("Workspace changed during search; search again.")
                found_items = [*items, *batch_items]
                found_total = total + batch_total
                found_errors = [*errors, *batch_errors][:8]
                return TaskPage(
                    found_items, found_total, offset, limit, offset + len(found_items) < found_total, counts,
                    found_errors, budget.limit_hit, complete and not budget.limit_hit and not found_errors,
                    task_sources=task_sources,
                )

            last_progress = None
            last_progress_total = 0

            def publish_progress(batch_items=(), batch_total=0, batch_errors=()):
                nonlocal last_progress, last_progress_total
                if on_progress is None:
                    return
                if cancelled.is_set():
                    raise CancelledError()
                if not total + batch_total and not errors and not batch_errors:
                    return
                now = time.monotonic()
                if last_progress is None or (not last_progress_total and total + batch_total) or now - last_progress >= 0.1:
                    on_progress(page(False, batch_items, batch_total, batch_errors))
                    last_progress = now
                    last_progress_total = total + batch_total

            # Stream matches within each batch while amortizing ripgrep launches.
            cursor = 0
            batch_size = (
                _STREAM_LOG_SEARCH_TASK_BATCH_SIZE
                if on_progress is not None
                else _LOG_SEARCH_TASK_BATCH_SIZE
            )
            while cursor < len(ordered) and not budget.limit_hit:
                batch = ordered[cursor:cursor + (batch_size if cursor else 1)]
                contexts = {}
                tasks_by_dir = {task["dir"]: task for task in batch}

                def start_task_context(task_dir, tasks_by_dir=tasks_by_dir, contexts=contexts):
                    task = tasks_by_dir[task_dir]
                    if task["name"] in contexts:
                        return
                    if cancelled.is_set():
                        raise CancelledError()
                    context = build_task_search_result(task, query, search_field=search_field, matcher=matcher,
                                                       max_count=budget.remaining)
                    contexts[task["name"]] = context
                    budget.take(context["match_count"])

                if not search_logs:
                    for task in batch:
                        start_task_context(task["dir"])
                        if budget.limit_hit:
                            break

                def collect_batch(log_results, batch=batch, contexts=contexts, base_total=total):
                    batch_items, batch_errors = [], []
                    batch_total = 0
                    for task in batch:
                        if cancelled.is_set():
                            raise CancelledError()
                        context = contexts.get(task["name"])
                        if context is None:
                            continue
                        logs = log_results.get(task["dir"], empty_logs)
                        batch_errors.extend(message for message in logs["errors"] if len(batch_errors) < 8)
                        if needles and not (context["match_count"] or logs["match_count"]):
                            continue
                        if offset <= base_total + batch_total < offset + limit:
                            current = manager.get_task(task["name"], summary=summary, compact=compact)
                            if current is None or manager._get_task_search_view(task["name"]) is not task:
                                # Keep contexts attached to the captured task if it
                                # disappears or a same-name replacement arrives.
                                if summary and compact:
                                    current = manager.serialize_task(dict(task), summary=True, compact=True)
                                else:
                                    current = manager.serialize_task({
                                        **task, "env": dict(task["env"]),
                                        "start_times": list(task["start_times"]),
                                        "finish_times": list(task["finish_times"]),
                                    }, summary=summary)
                            current["search_matches"] = context["matches"] + logs["matches"]
                            current["search_match_count"] = context["match_count"] + logs["match_count"]
                            batch_items.extend(_cap_summary_task_payloads([current]) if summary else [current])
                        batch_total += 1
                    return batch_items, batch_total, batch_errors

                if search_logs:
                    start_task_context(batch[0]["dir"])
                    if on_progress is not None:
                        publish_progress(*collect_batch({}))
                log_results = self._log_search.search_many(
                    [task["dir"] for task in batch], query, cancelled, matcher, budget=budget,
                    on_progress=(lambda logs: publish_progress(*collect_batch(logs))) if on_progress is not None else None,
                    on_task_start=start_task_context,
                ) if search_logs and not budget.limit_hit else {}
                batch_items, batch_total, batch_errors = collect_batch(log_results)
                items.extend(batch_items)
                total += batch_total
                errors.extend(batch_errors[:max(0, 8 - len(errors))])
                cursor += len(batch)
                if on_progress is not None and cursor < len(ordered) and not budget.limit_hit:
                    publish_progress()
            return page(True)
        finally:
            self._log_search.slots.release()

    @_with_stable_workspace
    def get_dashboard(
        self, *, refresh: bool = True, recent_limit: int = 6, include_workspace: bool = True,
    ) -> Dict[str, Any]:
        """Return lightweight dashboard data for the Home page."""

        page = self.list_tasks(limit=max(1, recent_limit), refresh=refresh, summary=True)
        summary = {
            "total": page.total,
            **{
                status: page.status_counts.get(status, 0)
                for status in ("running", "queued", "completed", "failed", "cancelled", "pending")
            },
        }
        active_task = next(
            (
                task
                for task in page.items
                if str(task.get("status", "")).lower() in {"running", "queued"}
            ),
            page.items[0] if page.items else None,
        )
        dashboard: Dict[str, Any] = {
            "summary": summary,
            "local_activity": self.task_manager.local_activity_counts(),
            "recent_tasks": page.items,
            "active_task": active_task,
        }
        if include_workspace:
            workspace = self.get_workspace_info()
            dashboard.update(workspace=workspace, template_count=len(workspace["templates"]))
        return dashboard

    @_with_stable_workspace
    def get_task(
        self, task_name: str, *, refresh: bool = True, summary: bool = False, include_tracks: bool = True,
    ) -> Dict[str, Any] | None:
        """Return one task snapshot."""
        manager = self.task_manager
        if refresh:
            manager.refresh_from_disk(task_ids=[task_name])
        task = manager.get_task(task_name, summary=summary, include_tracks=include_tracks)
        # A cold single-task read need not initialize the whole workspace.
        # After full discovery, refresh=False remains a cache-only lookup.
        if task is None and (refresh or not self._tasks_loaded):
            manager.load_task_by_name(task_name)
            task = manager.get_task(task_name, summary=summary, include_tracks=include_tracks)
        if task is not None and refresh and not os.path.isfile(
            os.path.join(str(task.get("dir", "") or ""), TASK_INFO_FILENAME)
        ):
            return None
        return task

    @_with_stable_workspace
    def get_run_input(self, task_name: str, run_index: int) -> Dict[str, Any]:
        """Load just one historical input, without loading curve data."""
        from pyruns.core.run_inputs import load_run_input
        from pyruns.utils.info_io import load_task_metadata

        task = self.require_task(task_name, refresh=True, summary=True, include_tracks=False)
        info = load_task_metadata(task["dir"], raise_error=True)
        if run_index < 1 or run_index > TaskManager._effective_run_slot_count(info):
            raise KeyError(f"{task_name}@{run_index}")
        return {
            "schema_version": 1, "task_name": task_name, "run_index": run_index,
            "input": load_run_input(task["dir"], run_index),
        }

    @_with_stable_workspace
    def compare_run_inputs(self, task_name: str, run_index: int, other_run: int) -> Dict[str, Any]:
        from pyruns.core.run_inputs import compare_run_inputs

        left = self.get_run_input(task_name, other_run)["input"]
        right = self.get_run_input(task_name, run_index)["input"]
        return compare_run_inputs(
            left, right, left_label=f"{task_name}@{other_run}", right_label=f"{task_name}@{run_index}",
        )

    @_with_stable_workspace
    def compare_runs(
        self, task_name: str, run_index: int, other_run: int, *, other_task: str | None = None,
    ) -> Dict[str, Any]:
        """Read two run summaries and their saved inputs without payloads or curves."""
        from pyruns.core.run_inputs import compare_run_inputs, load_run_input

        metadata: Dict[str, Dict[str, Any]] = {}

        def read(name: str, index: int) -> tuple[Dict[str, Any], Dict[str, Any] | None]:
            error = validate_task_name(name)
            if error:
                raise ValueError(error)
            task_dir = self.task_manager.task_directory(name)
            if name not in metadata:
                try:
                    metadata[name] = load_task_metadata(task_dir, raise_error=True)
                except FileNotFoundError as exc:
                    raise KeyError(name) from exc
            info = metadata[name]
            if index < 1 or index > TaskManager._effective_run_slot_count(info):
                raise KeyError(f"{name}@{index}")

            def at(key: str) -> Any:
                values = info.get(key, []) or []
                return values[index - 1] if index <= len(values) else None

            status = run_history_status(info, index)
            saved = load_run_input(task_dir, index)
            return {
                "task_name": name, "run_index": index, "reference": f"{name}@{index}",
                "status": status, "start_time": at("start_times") or None,
                "finish_time": at("finish_times") or None, "duration_seconds": at("durations"),
                "exit_code": at("exit_codes"), "source_state": at("source_states") or None,
                "environment": at("run_environments"), "record": at("records"),
                "input_available": saved is not None,
            }, saved

        left, left_input = read(task_name if other_task is None else other_task, other_run)
        right, right_input = read(task_name, run_index)
        return {
            "schema_version": 1, "left": left, "right": right,
            "metric_rows": _comparison_metric_rows(left["record"], right["record"]),
            "input_diff": compare_run_inputs(
                left_input, right_input, left_label=left["reference"], right_label=right["reference"],
            ),
        }

    def require_task(
        self, task_name: str, *, refresh: bool = True, summary: bool = False, include_tracks: bool = True,
    ) -> Dict[str, Any]:
        """Return one task or raise ``KeyError``."""
        task = self.get_task(task_name, refresh=refresh, summary=summary, include_tracks=include_tracks)
        if task is None:
            raise KeyError(task_name)
        return task

    @_with_stable_workspace
    def start_task(self, task_name: str, *, include_tracks: bool = True) -> Dict[str, Any]:
        """Start one task and return the updated snapshot."""
        self.ensure_tasks_loaded(full_refresh=False)
        task = self.require_task(task_name, summary=True)
        if task.get("_load_error"):
            raise ValueError(str(task["_load_error"]))
        self.invalidate_cache([task_name])
        started = (
            bool(self._submit_detached_runs([task_name], max_workers=1))
            if self.detached_task_runs else self.task_manager.start_task_now(task_name)
        )
        if not started:
            raise ValueError(f"Task '{task_name}' could not be started")
        return self.get_task(task_name, include_tracks=include_tracks) or task

    @_with_stable_workspace
    def cancel_task(self, task_name: str, *, include_tracks: bool = True, local_only: bool = False) -> Dict[str, Any]:
        """Request cancellation from the runner that owns the task."""
        manager = self.task_manager
        task = manager.get_task(task_name, summary=True)
        if task is None:
            task = manager.load_task_by_name(task_name)
        if task is None:
            raise KeyError(task_name)
        task_dir = str(task.get("dir", "") or "")
        info = load_task_metadata(task_dir) if task_dir else {}
        status = str(info.get("status", "") or "").lower()
        if status not in {"queued", "running"}:
            if status in {"completed", "failed", "cancelled"}:
                return self.get_task(task_name, include_tracks=include_tracks) or task
            raise ValueError(f"Task '{task_name}' cannot be cancelled")
        if local_only:
            remote_folder = task_storage_scope(task_dir) == "remote"
            if remote_folder or runner_location(info, manager.runner_host) == "remote":
                host = info.get("runner_host") or (device_folder_host(task_source(task_dir)) if remote_folder else "")
                raise ValueError(f"Stop this task on {host or 'the execution device'}.")
        requested_run_index = active_task_run_index(info)
        requested_attempt_id = str(info.get("_attempt_id", "") or "")
        self.invalidate_cache([task_name])
        ok = manager.request_task_cancel(
            task_name,
            expected_runner_id=str(info.get("runner_id", "") or ""),
            expected_run_index=requested_run_index,
            expected_attempt_id=requested_attempt_id,
        )
        if not ok:
            latest = self.get_task(task_name, include_tracks=include_tracks)
            # Verify the durable request and attempt against the state file.
            latest_info = load_task_metadata(task_dir) or {}
            latest_status = str(latest_info.get("status", "") or "").lower()
            if not task_attempt_matches(latest_info, requested_attempt_id):
                raise ValueError(f"Task '{task_name}' cannot be cancelled")
            if (
                latest_status in {"completed", "failed", "cancelled"}
                and active_task_run_index(latest_info) == requested_run_index
            ):
                return latest
            if (
                latest_status in {"queued", "running"}
                and active_task_run_index(latest_info) == requested_run_index
                and str(latest_info.get("runner_id", "") or "") == str(info.get("runner_id", "") or "")
                and latest_info.get("cancel_requested_at")
            ):
                return latest
            raise ValueError(f"Task '{task_name}' cannot be cancelled")
        return self.get_task(task_name, include_tracks=include_tracks) or task

    @_with_stable_workspace
    def start_tasks_batch(
        self,
        task_names: List[str],
        *,
        max_workers: int | None = None,
        include_tracks: bool = True,
        created_task_identities: Dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        """Queue multiple tasks and return their updated snapshots."""
        self.ensure_tasks_loaded(full_refresh=False)
        normalized_names: List[str] = []
        seen: set[str] = set()
        for name in task_names:
            task_name = str(name or "").strip()
            if not task_name or task_name in seen:
                continue
            task = self.require_task(task_name, refresh=False, summary=True)
            if task.get("_load_error"):
                raise ValueError(str(task["_load_error"]))
            if created_task_identities is not None and (
                not created_task_identities.get(task_name)
                or task.get("task_identity") != created_task_identities[task_name]
            ):
                raise ValueError(f"Task '{task_name}' changed since creation. Review it in Manager.")
            normalized_names.append(task_name)
            seen.add(task_name)

        if not normalized_names:
            raise ValueError("No valid tasks were provided for batch run.")

        self.invalidate_cache(normalized_names)
        if self.detached_task_runs:
            claimed_names = self._submit_detached_runs(
                normalized_names, max_workers=max_workers, identities=created_task_identities,
            )
        else:
            claimed_names = self.task_manager.start_batch_tasks(
                normalized_names,
                max_workers=max_workers,
                **({
                    "expected_run_indices": {name: 1 for name in normalized_names},
                    "expected_task_identities": created_task_identities,
                } if created_task_identities is not None else {}),
            )
        if not claimed_names:
            raise ValueError("None of the selected tasks could be started.")
        claimed = set(claimed_names)
        items = [
            self.get_task(task_name, refresh=True, include_tracks=include_tracks)
            or self.require_task(task_name, refresh=False, include_tracks=include_tracks)
            for task_name in claimed_names
        ]
        return {
            "count": len(items),
            "items": items,
            "skipped": [task_name for task_name in normalized_names if task_name not in claimed],
        }

    def _submit_detached_runs(
        self,
        names: list[str],
        *,
        max_workers: int | None,
        identities: dict[str, str] | None = None,
    ) -> list[str]:
        from pyruns.cli.runner import submit_cli_tasks

        manager = self.task_manager
        expected_runs = {}
        for name in names:
            task = self.require_task(name, refresh=False, summary=True)
            info = load_task_metadata(str(task.get("dir", "") or "")) or task
            expected_runs[name] = 1 if identities is not None else manager._next_run_index(info)
        result = submit_cli_tasks(
            manager,
            names,
            expected_runs=expected_runs,
            max_workers=max_workers,
            web_batch=True,
            task_identities={name: identities[name] for name in names} if identities is not None else None,
        )
        if result.status == "unresolved":
            raise ValueError("Start could not be confirmed. The runner may still be starting or running. Refresh task status before retrying.")
        return list(result.claimed) if result.status in {"accepted", "accepted_partial"} else []

    @_with_stable_workspace
    def delete_tasks_batch(self, task_names: List[str]) -> Dict[str, Any]:
        """Soft-delete multiple tasks."""
        self.ensure_tasks_loaded(full_refresh=False)
        normalized_names: List[str] = []
        seen: set[str] = set()
        for name in task_names:
            task_name = str(name or "").strip()
            if not task_name or task_name in seen:
                continue
            self.require_task(task_name, refresh=False, summary=True)
            normalized_names.append(task_name)
            seen.add(task_name)

        if not normalized_names:
            raise ValueError("No valid tasks were provided for batch delete.")

        self.invalidate_cache()
        deleted_names = self.task_manager.delete_tasks(normalized_names)
        if not deleted_names:
            raise ValueError("Could not move any selected tasks to trash.")
        return {
            "count": len(deleted_names),
            "deleted": deleted_names,
        }

    def _tasks_for_export(self, task_names: List[str]):
        self.ensure_tasks_loaded(full_refresh=False)
        names = dict.fromkeys(str(name or "").strip() for name in task_names)
        names.pop("", None)
        if not names:
            raise ValueError("No valid tasks were provided for export.")
        for name in names:
            task = self.get_task(name, refresh=True, summary=True)
            if task is None:
                raise KeyError(name)
            yield task

    @_with_stable_workspace
    def export_tasks_csv(self, task_names: List[str]) -> str:
        """Build a CSV export for the selected tasks."""
        csv_text = build_export_csv(list(self._tasks_for_export(task_names)))
        if not csv_text:
            raise ValueError("No monitor data available to export.")
        return csv_text

    @_with_stable_workspace
    def export_tasks_json(self, task_names: List[str], fields: set[str]) -> str:
        """Export task summaries and run data with opt-in attachments."""
        prefix = '{"tasks":['
        entries: List[str] = []
        size = len(prefix) + len("]}")
        for task in self._tasks_for_export(task_names):
            name = task["name"]
            info = load_export_metadata(task)
            runs = run_export_rows(name, info)
            entry = {
                "name": name,
                "kind": task.get("task_kind"),
                "status": info.get("status", task["status"]),
                "created_at": info.get("created_at"),
                "runs": runs,
            }
            if "environment" in fields:
                entry["run_environments"] = info.get("run_environments", [])
            if "notes" in fields:
                entry["notes"] = info.get("notes", "")
            if "config" in fields:
                config_file = task["config_file"]
                try:
                    text = read_task_payload_text(task["dir"], config_file)
                except (OSError, ValueError) as exc:
                    raise ValueError(f"Cannot export configuration for '{name}': {exc}") from exc
                entry["config"] = {"file": config_file, "text": text}
            if "logs" in fields:
                log = self.get_task_logs(name, tail_bytes=EXPORT_LOG_TAIL_BYTES, include_log_options=False)
                if log["selected_log"]:
                    entry["latest_log"] = {
                        "file": log["selected_log"], "text": log["content"], "truncated": log["tail_truncated"],
                    }
            encoded = json.dumps(entry, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            size += len(encoded.encode("utf-8")) + bool(entries)
            if size > MAX_JSON_EXPORT_BYTES:
                raise ValueError("Export exceeds 32 MiB. Select fewer tasks or fewer optional fields.")
            entries.append(encoded)
        return prefix + ",".join(entries) + "]}"

    @_with_stable_workspace
    def set_task_pin(
        self, task_name: str, pinned: bool | None = None, *, include_tracks: bool = True,
    ) -> Dict[str, Any]:
        """Toggle or set one task's pinned state."""
        self.require_task(task_name, refresh=False, summary=True)
        self.invalidate_cache([task_name])
        ok, result = self.task_manager.set_task_pinned(task_name, pinned)
        if not ok:
            if str(result) == "Task not found":
                raise KeyError(task_name)
            raise ValueError(str(result))
        return self.require_task(task_name, refresh=True, include_tracks=include_tracks)

    @_with_stable_workspace
    def reorder_tasks(
        self, items: List[Dict[str, Any]], *, include_tracks: bool = True, include_items: bool = True,
    ) -> Dict[str, Any]:
        """Persist manual card order for the Manager page."""
        self.ensure_tasks_loaded(full_refresh=False)
        self.invalidate_cache()
        ok, result = self.task_manager.reorder_tasks(items, include_tracks=include_tracks, include_items=include_items)
        if not ok:
            message = str(result)
            if message.startswith("Task not found: "):
                raise KeyError(message.split(": ", 1)[1])
            raise ValueError(message)
        if isinstance(result, int):
            return {"count": result, "items": []}
        return {
            "count": len(result),
            "items": result,
        }

    def pick_generator_shell_file(self) -> Dict[str, Any]:
        """Open a native shell script picker and return the selected script content."""

        if not native_picker_available():
            raise ValueError("Native file picker is unavailable on this server. Enter the path manually.")

        with self._workspace_lock:
            workspace_root = os.path.normcase(os.path.abspath(self.root_dir))
            workspace = self.get_workspace_info(include_templates=False)
        initial_dir = str(workspace.get("working_root", "") or workspace.get("project_root", "") or os.getcwd())
        shell_path = choose_shell_file(initial_dir)
        if not shell_path:
            raise ValueError("No shell script selected.")
        if not os.path.isfile(shell_path):
            raise FileNotFoundError(f"Shell script not found: {shell_path}")
        with self._workspace_lock:
            current_root = os.path.normcase(os.path.abspath(self.root_dir))
            if current_root != workspace_root:
                raise ValueError("Workspace changed while the file picker was open. Select the script again.")
            return self.get_template_content(shell_path)

    @_with_stable_workspace
    def update_task_notes(
        self,
        task_name: str,
        notes: str,
        expected_notes: str,
        *,
        include_tracks: bool = True,
        expected_task_identity: str | None = None,
    ) -> Dict[str, Any]:
        """Persist notes for one task."""
        self.require_task(task_name, refresh=False, summary=True)
        self.invalidate_cache([task_name])
        try:
            ok, result = self.task_manager.update_task_notes(
                task_name, notes, expected_notes, expected_task_identity=expected_task_identity,
            )
        except TaskStateConflict as exc:
            raise TaskNotesConflictError(str(exc)) from exc
        if not ok:
            if str(result) == "Task not found":
                raise KeyError(task_name)
            raise ValueError(str(result))
        return self.require_task(task_name, refresh=True, include_tracks=include_tracks)

    @_with_stable_workspace
    def update_task_env(
        self,
        task_name: str,
        env: Dict[str, Any],
        expected_env: Dict[str, Any],
        *,
        include_tracks: bool = True,
        expected_task_identity: str | None = None,
    ) -> Dict[str, Any]:
        """Persist env vars for one task."""
        self.require_task(task_name, refresh=False, summary=True)
        self.invalidate_cache([task_name])
        try:
            ok, result = self.task_manager.update_task_env(
                task_name, env, expected_env, expected_task_identity=expected_task_identity,
            )
        except TaskStateConflict as exc:
            raise TaskEnvConflictError(str(exc)) from exc
        if not ok:
            if str(result) == "Task not found":
                raise KeyError(task_name)
            raise ValueError(str(result))
        return self.require_task(task_name, refresh=True, include_tracks=include_tracks)

    @_with_stable_workspace
    def rename_task(self, task_name: str, new_name: str, *, include_tracks: bool = True) -> Dict[str, Any]:
        """Rename one task."""
        self.require_task(task_name, refresh=False, summary=True)
        self.invalidate_cache()
        ok, result = self.task_manager.rename_task(task_name, new_name)
        if not ok:
            message = str(result)
            if message == "Task not found":
                raise KeyError(task_name)
            raise ValueError(message)
        renamed = str(result)
        return self.require_task(renamed, refresh=True, include_tracks=include_tracks)

    @_with_stable_workspace
    def get_task_logs(
        self,
        task_name: str,
        *,
        log_file_name: str | None = None,
        offset: int | None = None,
        log_identity: str | None = None,
        tail_bytes: int | None = None,
        tail_lines: int | None = None,
        known_preview: str | None = None,
        chunk_size: int | None = None,
        include_log_options: bool = True,
        expected_workspace_root: str | None = None,
        expected_task_dir: str | None = None,
        strict: bool = False,
    ) -> Dict[str, Any]:
        """Load a historical log chunk for the monitor page."""
        if expected_workspace_root is not None:
            current_root = os.path.normcase(os.path.abspath(self.root_dir))
            expected_root = os.path.normcase(os.path.abspath(expected_workspace_root))
            if current_root != expected_root:
                raise WorkspaceChangedError("Workspace changed")
        task = self.get_task(task_name, refresh=False, summary=True)
        if task is None:
            raise KeyError(task_name)

        task_dir = str(task["dir"])
        if expected_task_dir is not None:
            current_task_dir = os.path.normcase(os.path.abspath(task_dir))
            expected_dir = os.path.normcase(os.path.abspath(expected_task_dir))
            if current_task_dir != expected_dir:
                raise WorkspaceChangedError("Workspace changed")
        selected_name = str(log_file_name or "").strip()
        if not include_log_options and offset is not None and selected_name:
            # A stream has already selected the file; listing every old run on each chunk is costly.
            log_options = {}
            available_logs = []
            valid_name = (
                selected_name in {_cfg.QUEUE_LOG_FILENAME, _cfg.ERROR_LOG_FILENAME}
                or selected_name.startswith("run") and selected_name.endswith(".log")
            )
            try:
                selected_path = validate_task_log_path(task_dir, selected_name) if valid_name else None
            except ValueError:
                selected_path = None
            if selected_path and not os.path.isfile(selected_path):
                selected_path = None
        else:
            log_options = get_log_options(task_dir)
            available_logs = list(log_options.keys())
            selected_path = log_options.get(selected_name) if selected_name else None

        task_status = str(task.get("status", "")).lower()
        queue_name = _cfg.QUEUE_LOG_FILENAME
        active_run_name = ""
        if task_status == "running":
            try:
                run_index = max(1, int(task.get("run_index", 1) or 1))
            except (TypeError, ValueError):
                run_index = 1
            active_run_name = f"run{run_index}.log"
        if not selected_name and task_status == "queued" and queue_name in log_options:
            selected_name = queue_name
            selected_path = log_options.get(queue_name)

        if not selected_name and active_run_name:
            selected_name = active_run_name
            selected_path = log_options.get(selected_name)

        if selected_name and selected_name == active_run_name and selected_name not in available_logs:
            available_logs.insert(0, selected_name)

        if not selected_name:
            selected_path = resolve_log_path(task_dir, options=log_options)
            selected_name = os.path.basename(selected_path) if selected_path else ""

        if strict and not selected_path and selected_name:
            # isfile/exists deliberately suppress permission and remote I/O
            # errors. Streams must distinguish those errors from a log which
            # has not been created yet, so the health protocol can report them.
            candidate = validate_task_log_path(task_dir, selected_name)
            try:
                os.stat(candidate)
            except FileNotFoundError:
                pass
            else:
                selected_path = candidate

        if not selected_path:
            return {
                "task_name": task_name,
                "selected_log": selected_name,
                "available_logs": available_logs,
                "content": "",
                "offset": 0,
                "reset": bool(offset is not None and log_identity),
                "log_identity": "",
                "tail_truncated": False,
                "tail_byte_limited": False,
                "tail_limit_bytes": MAX_MONITOR_LOG_TAIL_BYTES,
            }

        tail_truncated = False
        tail_byte_limited = False
        tail_limit_bytes = MAX_MONITOR_LOG_TAIL_BYTES
        selected_identity = ""
        if offset is not None or tail_lines is None:
            selected_identity = log_file_identity(selected_path, **({"strict": True} if strict else {}))
        preview_revision = ""
        reset = False
        file_size = None
        if offset is None:
            if tail_lines is not None:
                requested_tail_bytes = (
                    MAX_MONITOR_LOG_TAIL_BYTES
                    if tail_bytes is None
                    else max(1, int(tail_bytes))
                )
                tail_limit_bytes = min(MAX_MONITOR_LOG_TAIL_BYTES, requested_tail_bytes)
                preview = read_log_preview(
                    selected_path,
                    max_lines=max(0, tail_lines),
                    max_bytes=tail_limit_bytes,
                    known_revision=known_preview,
                    **({"strict": True} if strict else {}),
                )
                content, new_offset = preview.content, preview.offset
                tail_truncated, tail_byte_limited = preview.truncated, preview.byte_limited
                selected_identity = preview.identity or log_file_identity(selected_path, strict=strict)
                preview_revision = preview.revision
            else:
                byte_limit = tail_bytes
                if byte_limit is None:
                    byte_limit = _int_setting(
                        self.settings,
                        "monitor_chunk_size",
                        _cfg.DEFAULT_MONITOR_CHUNK_SIZE,
                    )
                tail_limit_bytes = min(MAX_MONITOR_LOG_TAIL_BYTES, max(1, int(byte_limit)))
                content, new_offset = read_last_bytes(
                    selected_path, n_bytes=tail_limit_bytes, **({"strict": True} if strict else {}),
                )
                try:
                    tail_truncated = os.path.getsize(selected_path) > tail_limit_bytes
                except OSError:
                    tail_truncated = False
                tail_byte_limited = tail_truncated
        else:
            byte_limit = chunk_size
            if byte_limit is None:
                byte_limit = _int_setting(
                    self.settings,
                    "monitor_chunk_size",
                    _cfg.DEFAULT_MONITOR_CHUNK_SIZE,
                )
            requested_offset = max(0, offset)
            try:
                selected_size = os.path.getsize(selected_path)
            except OSError:
                if strict:
                    raise
                selected_size = 0
            identity_changed = bool(
                log_identity
                and selected_identity
                and str(log_identity) != selected_identity
            )
            reset = identity_changed or requested_offset > selected_size
            read_offset = 0 if reset else requested_offset
            content, new_offset = safe_read_log(
                selected_path, read_offset, max_bytes=max(1, byte_limit),
                **({"strict": True} if strict else {}),
            )
            file_size = max(selected_size, new_offset)

        return {
            "task_name": task_name,
            "selected_log": selected_name,
            "available_logs": available_logs,
            "content": content,
            "offset": new_offset,
            "reset": reset,
            "log_identity": selected_identity,
            "tail_truncated": tail_truncated,
            "tail_byte_limited": tail_byte_limited,
            "tail_limit_bytes": tail_limit_bytes,
            **({"preview_revision": preview_revision, "preview_unchanged": preview_revision == known_preview}
               if preview_revision else {}),
            **({"file_size": file_size} if file_size is not None else {}),
        }

    @_with_stable_workspace
    def open_task_log_download(
        self, task_name: str, log_file_name: str, *, log_identity: str | None = None,
        expected_workspace_root: str | None = None,
    ) -> OpenedTaskLog:
        """Open and pin one validated log; callers own and must close the handle."""
        self._check_expected_workspace(expected_workspace_root)
        task = self.get_task(task_name, refresh=False, summary=True)
        if task is None:
            raise KeyError(task_name)
        if log_file_name not in {_cfg.QUEUE_LOG_FILENAME, _cfg.ERROR_LOG_FILENAME} and not (
            log_file_name.startswith("run") and log_file_name.endswith(".log")
        ):
            raise ValueError("Select a task log file")
        task_dir = str(task["dir"])
        path = validate_task_log_path(task_dir, log_file_name)
        before = os.stat(path, follow_symlinks=False)

        def opener(filename: str, flags: int) -> int:
            return regular_file_opener(filename, flags | getattr(os, "O_NOFOLLOW", 0))

        handle = open(path, "rb", opener=opener)
        try:
            opened = os.fstat(handle.fileno())
            # Revalidate directory boundaries and the path after opening. Stream
            # this handle, never reopen the path after the identity check.
            current_path = validate_task_log_path(task_dir, log_file_name)
            current = os.stat(current_path, follow_symlinks=False)
            if not os.path.samestat(before, opened) or not os.path.samestat(current, opened):
                raise LogFileChangedError("This log changed. Refresh it before downloading.")
            identity = f"{opened.st_dev:x}:{opened.st_ino:x}" if opened.st_ino > 0 else ""
            if log_identity and identity != log_identity:
                raise LogFileChangedError("This log changed. Refresh it before downloading.")
            return OpenedTaskLog(handle, f"{task_name}--{log_file_name}", opened.st_size, identity)
        except BaseException:
            handle.close()
            raise

    def get_metrics(
        self,
        *,
        include_processes: bool = False,
        detail: bool = False,
    ) -> Dict[str, Any]:
        """Return summary metrics with optional GPU and process details."""
        return self.metrics_sampler.sample(
            include_processes=include_processes,
            detail=detail,
        )

    def get_process_details(self, pid: int) -> Dict[str, Any]:
        """Return OS metadata for one explicitly selected process."""

        return self.metrics_sampler.get_process_details(pid)

    @_with_stable_workspace
    def open_shell_workspace(self, *, include_templates: bool = True) -> Dict[str, Any]:
        """Prepare and activate the project-level shell workspace."""

        with self._workspace_lock:
            self._check_expected_workspace()
            current_root = self.root_dir or os.getenv(_cfg.ENV_KEY_ROOT, _cfg.ROOT_DIR)
        shell_root = bootstrap_shell_workspace(current_root)
        self.reload(shell_root)
        return self.get_workspace_info(include_templates=include_templates)

    @_with_stable_workspace
    def create_tasks_from_template(
        self,
        *,
        name_prefix: str,
        mode: str,
        yaml_text: str = "",
        shell_text: str = "",
        template_value: str = "",
        append_timestamp: bool = True,
        task_storage: str | None = None,
    ) -> Dict[str, Any]:
        """Create one or many tasks from script-yaml or shell input."""

        normalized_prefix = str(name_prefix or "").strip() or "task"
        if append_timestamp:
            normalized_prefix = f"{normalized_prefix}_{get_now_str()}"

        default_storage = self.settings.get("task_storage", "shared")
        storage = default_storage if task_storage is None else task_storage
        root = new_task_root(self.tasks_dir, storage)
        err = validate_task_name(normalized_prefix, root)
        if err:
            raise ValueError(err)

        generator = self.task_generator if storage == default_storage else self._task_generator_factory(root)
        workspace_kind = normalize_workspace_kind(load_script_info(self.root_dir).get("workspace_kind"))
        editor_mode = str(mode or "form").strip().lower()

        if workspace_kind == _cfg.WORKSPACE_KIND_SHELL:
            if editor_mode != "shell":
                raise ValueError("Shell workspace only supports shell mode.")
            content = _require_bounded_editor_text(shell_text, label="Shell content")
            if not content.strip():
                raise ValueError("Shell mode requires non-empty script content.")
            task = generator.create_shell_task(normalized_prefix, content)
            self.invalidate_cache()
            self.task_manager.add_task(task)
            page = self.list_tasks(limit=12, refresh=False, summary=True)
            return {
                "count": 1,
                "items": [self.task_manager.serialize_task(task, summary=False)],
                "recent_tasks": page.items,
                "task_kind": TASK_KIND_SHELL,
            }

        if editor_mode not in {"form", "yaml"}:
            raise ValueError(f"Unsupported generator mode: {mode}")

        try:
            base_config = load_config_text(
                _require_bounded_editor_text(yaml_text, label="YAML content")
            )
        except (ValueError, yaml.YAMLError) as exc:
            raise ValueError(f"Invalid YAML: {exc}") from exc
        if not isinstance(base_config, DictConfig):
            raise ValueError("YAML content must be a mapping at the root.")

        if template_value:
            orig_config = self._load_generator_template_config(template_value)
        else:
            orig_config = {}

        if editor_mode == "form":
            try:
                configs = generate_batch_configs(base_config)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
        else:
            if count_batch_configs(base_config) != 1:
                raise ValueError("YAML mode does not support batch syntax. Switch to Grid or Tree mode.")
            configs = [base_config]

        if template_value and orig_config:
            err_msg = validate_config_types_against_template(orig_config, configs)
            if err_msg:
                raise ValueError(err_msg)

        tasks = generator.create_tasks(
            configs,
            normalized_prefix,
            task_kind=TASK_KIND_CONFIG,
        )
        self.invalidate_cache()
        self.task_manager.add_tasks(tasks)
        page = self.list_tasks(limit=12, refresh=False, summary=True)
        return {
            "count": len(tasks),
            "items": [
                self.task_manager.serialize_task(item, summary=False)
                for item in tasks
            ],
            "recent_tasks": page.items,
            "task_kind": TASK_KIND_CONFIG,
        }

    @_with_stable_workspace
    def preview_tasks_from_template(
        self,
        *,
        mode: str,
        yaml_text: str = "",
        shell_text: str = "",
        template_value: str = "",
    ) -> Dict[str, Any]:
        """Preview task expansion without creating tasks."""

        workspace_kind = normalize_workspace_kind(load_script_info(self.root_dir).get("workspace_kind"))
        editor_mode = str(mode or "form").strip().lower()

        if workspace_kind == _cfg.WORKSPACE_KIND_SHELL:
            if editor_mode != "shell":
                raise ValueError("Shell workspace only supports shell mode.")
            content = _require_bounded_editor_text(shell_text, label="Shell content")
            if not content.strip():
                raise ValueError("Shell mode requires non-empty script content.")
            preview_text, _ = build_task_preview_and_search(
                task_kind=TASK_KIND_SHELL,
                config_text=content,
            )
            return {
                "count": 1,
                "items": [
                    {
                        "index": 1,
                        "preview": preview_text,
                        "config": {},
                        "config_text": content,
                    }
                ],
                "task_kind": TASK_KIND_SHELL,
            }

        if editor_mode not in {"form", "yaml"}:
            raise ValueError(f"Unsupported generator mode: {mode}")

        try:
            base_config = load_config_text(
                _require_bounded_editor_text(yaml_text, label="YAML content")
            )
        except (ValueError, yaml.YAMLError) as exc:
            raise ValueError(f"Invalid YAML: {exc}") from exc
        if not isinstance(base_config, DictConfig):
            raise ValueError("YAML content must be a mapping at the root.")

        if template_value:
            orig_config = self._load_generator_template_config(template_value)
        else:
            orig_config = {}

        if editor_mode == "form":
            try:
                total_count, configs = preview_batch_configs(base_config, template_config=orig_config)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
        else:
            if count_batch_configs(base_config) != 1:
                raise ValueError("YAML mode does not support batch syntax. Switch to Grid or Tree mode.")
            configs = [base_config]
            total_count = 1
            if template_value and orig_config:
                err_msg = validate_config_types_against_template(orig_config, configs)
                if err_msg:
                    raise ValueError(err_msg)

        preview_items: List[Dict[str, Any]] = []
        for index, config in enumerate(configs, start=1):
            container = to_container(config, resolve=False)
            preview_items.append(
                {
                    "index": index,
                    "preview": preview_config_line(config),
                    "config": container,
                    "config_text": dump_config_text(container),
                }
            )

        return {
            "count": total_count,
            "items": preview_items,
            "task_kind": TASK_KIND_CONFIG,
        }

    def list_launcher_scripts(self) -> List[Dict[str, Any]]:
        """Return launchable scripts from the current project directory."""
        with self._workspace_lock:
            self._check_expected_workspace()
            run_root = os.path.abspath(self.root_dir)
        parent = os.path.dirname(run_root)
        if os.path.basename(run_root) == _cfg.DEFAULT_ROOT_NAME:
            project_dir = parent
        elif os.path.basename(parent) == _cfg.DEFAULT_ROOT_NAME:
            project_dir = os.path.dirname(parent)
        else:
            project_dir = os.getcwd()
        return list_script_candidates(project_dir)

    def list_launcher_configs(self, script_path: str) -> List[Dict[str, Any]]:
        """Return YAML config candidates for a selected script."""

        return list_config_candidates(script_path)

    def get_launcher_config_info(self, script_path: str) -> Dict[str, Any]:
        """Return config candidates plus first-launch metadata for a script."""

        metadata = get_config_selection_metadata(script_path)
        return {
            "items": list_config_candidates(script_path),
            **metadata,
        }

    def list_launcher_workspaces(
        self,
        script_path: str,
        config_path: str | None = None,
    ) -> List[Dict[str, Any]]:
        """Return inferred workspace candidates for the launcher."""

        return list_workspace_candidates(script_path, config_path)

    @_with_stable_workspace
    def open_launcher_workspace(
        self,
        script_path: str,
        config_path: str | None = None,
        *,
        include_templates: bool = True,
    ) -> Dict[str, Any]:
        """Prepare and activate a workspace selected in the launcher."""

        workspace = bootstrap_workspace(script_path, config_path or None)
        self.reload(workspace)
        return self.get_workspace_info(include_templates=include_templates)

    def validate_launcher_path(self, kind: str, path: str, script_path: str | None = None) -> Dict[str, Any]:
        """Validate a manually entered launcher path without changing workspace state."""

        path_text = str(path or "").strip()
        kind_text = str(kind or "").strip().lower()
        if not path_text:
            return {
                "ok": False,
                "kind": kind_text,
                "normalized_path": "",
                "path_type": "",
                "message": "Path is empty.",
            }

        normalized = normalize_path(path_text)

        if kind_text in {"python", "script", "py"}:
            ok = os.path.isfile(normalized) and normalized.lower().endswith(".py")
            return {
                "ok": ok,
                "kind": "python",
                "normalized_path": normalized,
                "path_type": "file" if os.path.isfile(normalized) else "",
                "message": "Python script found." if ok else f"Python script does not exist or is not a .py file: {path_text}",
            }

        if kind_text in {"shell", "folder", "directory", "dir"}:
            ok = os.path.isdir(normalized)
            return {
                "ok": ok,
                "kind": "shell",
                "normalized_path": normalized,
                "path_type": "directory" if ok else "",
                "message": "Shell folder found." if ok else f"Shell folder does not exist: {path_text}",
            }

        if kind_text in {"config", "yaml", "yml"}:
            config_path = normalized
            if script_path and not os.path.isabs(path_text):
                script_normalized = normalize_path(script_path)
                if os.path.isfile(script_normalized) and script_normalized.lower().endswith(".py"):
                    config_path = normalize_path(os.path.join(os.path.dirname(script_normalized), path_text))
            suffix = os.path.splitext(config_path)[1].lower()
            ok = os.path.isfile(config_path) and suffix in {".yaml", ".yml"}
            return {
                "ok": ok,
                "kind": "config",
                "normalized_path": config_path,
                "path_type": "file" if os.path.isfile(config_path) else "",
                "message": "YAML config found." if ok else f"YAML config does not exist or is not a .yaml/.yml file: {path_text}",
            }

        return {
            "ok": False,
            "kind": kind_text,
            "normalized_path": normalized,
            "path_type": "",
            "message": f"Unsupported launcher path kind: {kind}",
        }

    def pick_launcher_script_path(self) -> Dict[str, Any]:
        """Open a native script picker and return the selected script without bootstrapping."""

        if not native_picker_available():
            raise ValueError("Native file picker is unavailable on this server. Enter the path manually.")

        current_script = self.get_workspace_info(include_templates=False).get("script_path", "")
        initial_dir = os.path.dirname(str(current_script or "")) or os.getcwd()
        script_path = choose_script_file(initial_dir)
        if not script_path:
            raise ValueError("No script selected.")
        return list_workspace_candidates(script_path)[0]

    def pick_launcher_config_path(self, script_path: str) -> Dict[str, Any]:
        """Open a native YAML picker and return the selected config without bootstrapping."""

        if not native_picker_available():
            raise ValueError("Native file picker is unavailable on this server. Enter the path manually.")

        normalized_script = self.validate_launcher_path("python", script_path)
        if not normalized_script["ok"]:
            raise FileNotFoundError(str(normalized_script["message"]))

        script_dir = os.path.dirname(str(normalized_script["normalized_path"]))
        config_dir = os.path.join(script_dir, "configs")
        initial_dir = config_dir if os.path.isdir(config_dir) else script_dir
        config_path = choose_config_file(initial_dir)
        if not config_path:
            raise ValueError("No YAML config selected.")

        validation = self.validate_launcher_path("config", config_path)
        if not validation["ok"]:
            raise FileNotFoundError(str(validation["message"]))

        normalized_path = str(validation["normalized_path"])
        return {
            "path": normalized_path,
            "label": os.path.basename(normalized_path),
            "kind": "manual",
        }

    def pick_and_open_launcher_workspace(self, *, include_templates: bool = True) -> Dict[str, Any]:
        """Open a native script picker and activate the chosen workspace."""

        if not native_picker_available():
            raise ValueError("Native file picker is unavailable on this server. Enter the path manually.")

        current_script = self.get_workspace_info(include_templates=False).get("script_path", "")
        initial_dir = os.path.dirname(str(current_script or "")) or os.getcwd()
        script_path = choose_script_file(initial_dir)
        if not script_path:
            raise ValueError("No script selected.")
        return self.open_launcher_workspace(script_path, include_templates=include_templates)

    def pick_and_open_shell_workspace(self, *, include_templates: bool = True) -> Dict[str, Any]:
        """Open a native folder picker and activate that directory's shell workspace."""

        if not native_picker_available():
            raise ValueError("Native folder picker is unavailable on this server. Enter the path manually.")

        current_root = self.root_dir or os.getenv(_cfg.ENV_KEY_ROOT, _cfg.ROOT_DIR)
        if current_root:
            initial_dir = os.path.dirname(os.path.dirname(str(current_root)))
        else:
            initial_dir = os.getcwd()

        selected_dir = choose_directory(initial_dir)
        if not selected_dir:
            raise ValueError("No directory selected.")

        return self.open_shell_workspace_at(selected_dir, include_templates=include_templates)

    @_with_stable_workspace
    def open_shell_workspace_at(self, project_dir: str, *, include_templates: bool = True) -> Dict[str, Any]:
        """Activate a shell workspace for a user-entered project directory."""

        selected_dir = normalize_path(project_dir)
        if not os.path.isdir(selected_dir):
            raise ValueError(f"Shell folder does not exist: {project_dir}")

        project_root = normalize_path(os.path.join(selected_dir, _cfg.DEFAULT_ROOT_NAME))
        workspace = bootstrap_shell_workspace(project_root)
        self.reload(workspace)
        return self.get_workspace_info(include_templates=include_templates)
