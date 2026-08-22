"""Shared runtime state for the Pyruns API server."""

from __future__ import annotations

import math
import os
import shutil
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable, Dict, List

import yaml
from omegaconf import DictConfig

import pyruns._config as _cfg
from pyruns._config import (
    CONFIG_DEFAULT_FILENAME,
    SCRIPT_INFO_FILENAME,
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
from pyruns.core.report import build_export_csv
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
from pyruns.utils.batch_utils import count_batch_configs, generate_batch_configs
from pyruns.utils.config_utils import (
    load_config_text,
    list_template_files,
    preview_config_line,
    to_container,
    validate_config_types_against_template,
)
from pyruns.utils.info_io import (
    get_log_options,
    load_script_info,
    load_task_info,
    resolve_log_path,
    validate_task_name,
    validate_tasks_root,
    validate_workspace_directory,
)
from pyruns.utils.log_io import log_file_identity, read_last_bytes, read_last_lines, safe_read_log
from pyruns.utils.process_utils import hidden_subprocess_kwargs
from pyruns.utils.settings import ensure_settings_file, load_settings, save_settings_for_root
from pyruns.utils.shell_runtime import get_shell_runtime_for_workspace
from pyruns.utils.sort_utils import filter_tasks, sort_tasks_for_manager
from pyruns.utils.task_files import (
    MAX_TASK_PAYLOAD_BYTES,
    build_task_preview_and_search,
    normalize_task_kind,
    normalize_workspace_kind,
    resolve_task_payload_path,
)


_DEFAULT_GPU_SCHEDULER_CONFIG = GpuSchedulerConfig.from_settings({})
MAX_MONITOR_LOG_TAIL_BYTES = _cfg.MAX_MONITOR_CHUNK_SIZE
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
    "memory_used_pct": "gpu_scheduler_memory_used_pct",
    "min_free_memory_gb": "gpu_scheduler_min_free_memory_gb",
    "compute_used_pct": "gpu_scheduler_compute_used_pct",
    "stable_seconds": "gpu_scheduler_stable_seconds",
    "max_wait_seconds": "gpu_scheduler_max_wait_seconds",
    "max_tasks_per_gpu": "gpu_scheduler_max_tasks_per_gpu",
    "respect_cuda_visible_devices": "gpu_scheduler_respect_cuda_visible_devices",
    "require_same_gpu_model": "gpu_scheduler_require_same_gpu_model",
}


class WorkspaceChangedError(RuntimeError):
    """Raised when a workspace-bound stream outlives its source workspace."""


class TaskNotesConflictError(RuntimeError):
    """Raised when task notes changed after an editor loaded them."""


class TaskEnvConflictError(RuntimeError):
    """Raised when task environment changed after an editor loaded it."""


def _with_stable_workspace(method: Callable[..., Any]) -> Callable[..., Any]:
    """Keep one workspace active for the full runtime operation."""

    @wraps(method)
    def wrapped(self: "PyrunsRuntime", *args: Any, **kwargs: Any) -> Any:
        with self._workspace_lock:
            return method(self, *args, **kwargs)

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
        elif key == "max_tasks_per_gpu":
            clean[setting_key] = _coerce_int_payload(
                value,
                _DEFAULT_GPU_SCHEDULER_CONFIG.max_tasks_per_gpu,
                minimum=1,
            )
        elif key == "max_wait_seconds":
            clean[setting_key] = _coerce_float_payload(
                value,
                _DEFAULT_GPU_SCHEDULER_CONFIG.max_wait_seconds,
                minimum=1.0,
            )
        elif key in {"memory_used_pct", "compute_used_pct"}:
            default = (
                _DEFAULT_GPU_SCHEDULER_CONFIG.memory_used_pct
                if key == "memory_used_pct"
                else _DEFAULT_GPU_SCHEDULER_CONFIG.compute_used_pct
            )
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
                minimum=1.0,
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
        self._task_manager_factory = (
            task_manager_factory
            if task_manager_factory is not None
            else lambda tasks_dir: TaskManager(tasks_dir=tasks_dir, lazy_scan=False)
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
        self._last_full_refresh_time = 0.0
        self._conda_envs_cache: Dict[str, Any] | None = None
        self.reload(root_dir)

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
            self._workspace_epoch += 1
            self.root_dir = resolved_root
            self.tasks_dir = tasks_dir
            self.settings = load_settings(resolved_root)
            self._task_manager = self._task_managers.get(manager_key)
            self._task_generator = None
            self._metrics_sampler = None
            self._tasks_loaded = False
            self._last_full_refresh_time = 0.0
            self._conda_envs_cache = None

            cached_managers = list(self._task_managers.items())

        for cached_key, cached_manager in cached_managers:
            if cached_key != manager_key:
                self._retire_task_manager_if_idle(cached_key, cached_manager)

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
            for task in tasks
        )

    @_with_stable_workspace
    def active_task_count(self) -> int:
        """Synchronously count active work across every manager owned by this UI."""

        self.ensure_tasks_loaded(full_refresh=False)
        with self._lock:
            managers = list(dict.fromkeys(self._task_managers.values()))
            if self._task_manager is not None and self._task_manager not in managers:
                managers.append(self._task_manager)

        active_count = 0
        for manager in managers:
            manager.refresh_from_disk(force_all=True, discover=True)
            manager_count = sum(
                1
                for task in manager.list_tasks(summary=True)
                if str(task.get("status", "") or "").lower() in {"queued", "running"}
            )
            if manager_count == 0 and bool(getattr(manager, "is_processing", False)):
                manager_count = 1
            active_count += manager_count
        return active_count

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
    def change_run_root(self, new_root: str) -> Dict[str, Any]:
        """Switch the active workspace after validation."""
        resolved_root = self._normalize_path(new_root)
        if not self._has_workspace_markers(resolved_root):
            raise ValueError(
                f"Run Root must contain {TASKS_DIR}/, {CONFIG_DEFAULT_FILENAME}, or {SCRIPT_INFO_FILENAME}"
            )
        self.reload(resolved_root)
        return self.get_workspace_info()

    @_with_stable_workspace
    def get_workspace_info(self) -> Dict[str, Any]:
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
        return {
            "run_root": self.root_dir,
            "working_root": working_root,
            "native_file_picker": native_picker_available(),
            "tasks_dir": self.tasks_dir,
            "script_path": script_path,
            "script_name": str(script_info.get("script_name", "") or ""),
            "config_default_source": str(script_info.get("config_default_source", "") or ""),
            "config_default_source_name": str(script_info.get("config_default_source_name", "") or ""),
            "project_root": project_root,
            "workspace_kind": workspace_kind,
            "workspace_ready": workspace_ready,
            "settings": dict(self.settings),
            "shell_runtime": get_shell_runtime_for_workspace(self.root_dir),
            "templates": self.list_templates(),
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
            "python_executable": self._clean_setting_text(settings.get("python_executable")),
            "conda_env": self._clean_setting_text(settings.get("conda_env")),
            "conda_executable": self._clean_setting_text(settings.get("conda_executable")) or "conda",
            "global_env": {str(k): str(v) for k, v in global_env.items()},
            "gpu_scheduler": {
                "enabled": gpu_config.enabled,
                "task_mode": "multi" if gpu_config.task_mode == "multi" else "single",
                "selection_mode": "specified" if gpu_config.uses_specified_devices else "auto",
                "gpus_per_task": gpu_config.gpus_per_task,
                "device_ids": list(gpu_config.device_ids),
                "memory_used_pct": gpu_config.memory_used_pct,
                "min_free_memory_gb": gpu_config.min_free_memory_gb,
                "compute_used_pct": gpu_config.compute_used_pct,
                "stable_seconds": gpu_config.stable_seconds,
                "max_wait_seconds": gpu_config.max_wait_seconds,
                "max_tasks_per_gpu": gpu_config.max_tasks_per_gpu,
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

        allowed = {"python_executable", "conda_env", "conda_executable", "global_env", "global_env_text", "gpu_scheduler"}
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
        if provider_settings_changed:
            self._conda_envs_cache = None
        return self.get_runtime_info(refresh_providers=refresh_providers)

    @_with_stable_workspace
    def list_templates(self) -> List[Dict[str, str]]:
        """Return loadable template options for the Generator page."""
        script_info = load_script_info(self.root_dir)
        if normalize_workspace_kind(script_info.get("workspace_kind")) == _cfg.WORKSPACE_KIND_SHELL:
            return self.list_shell_templates(script_info)
        options = list_template_files(self.root_dir)
        source_name = str(script_info.get("config_default_source_name", "") or "")
        result = []
        for value, label in options.items():
            display_label = label
            if source_name and os.path.basename(value) == CONFIG_DEFAULT_FILENAME:
                display_label = f"{label} (from {source_name})"
            result.append({"value": value, "label": display_label})
        return result

    @_with_stable_workspace
    def list_shell_templates(self, script_info: Dict[str, Any] | None = None) -> List[Dict[str, str]]:
        """Return existing shell task payloads that can seed shell-mode tasks."""

        items: List[Dict[str, str]] = []

        tasks_dir = os.path.join(self.root_dir, TASKS_DIR)
        if os.path.isdir(tasks_dir):
            task_entries: List[Dict[str, Any]] = []
            for dir_name in sorted(os.listdir(tasks_dir)):
                if dir_name.startswith("."):
                    continue
                task_dir = os.path.join(tasks_dir, dir_name)
                if not os.path.isdir(task_dir):
                    continue
                task_info = load_task_info(task_dir)
                if normalize_task_kind(task_info.get("task_kind")) != _cfg.TASK_KIND_SHELL:
                    continue

                configured_file = str(task_info.get("config_file", "") or "")
                candidates = [configured_file] if configured_file else []
                candidates.extend(name for name in _cfg.SHELL_CONFIG_FILENAMES if name not in candidates)

                payload_name = ""
                for name in candidates:
                    if not name:
                        continue
                    try:
                        payload_path = resolve_task_payload_path(task_dir, name)
                    except ValueError:
                        continue
                    if os.path.isfile(payload_path):
                        payload_name = os.path.relpath(payload_path, task_dir)
                        break
                if not payload_name:
                    continue

                value = os.path.join(TASKS_DIR, dir_name, payload_name).replace("\\", "/")
                try:
                    fallback_mtime = os.path.getmtime(os.path.join(task_dir, _cfg.TASK_INFO_FILENAME))
                except OSError:
                    try:
                        fallback_mtime = os.path.getmtime(os.path.join(task_dir, payload_name))
                    except OSError:
                        fallback_mtime = 0.0
                task_entries.append(
                    {
                        "name": dir_name,
                        "status": task_info.get("status", "pending"),
                        "created_at": task_info.get("created_at")
                        or (
                            time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(fallback_mtime))
                            if fallback_mtime
                            else ""
                        ),
                        "start_times": task_info.get("start_times", []),
                        "finish_times": task_info.get("finish_times", []),
                        "pinned": task_info.get("pinned", False),
                        "task_order": task_info.get("task_order"),
                        "_template_path": value,
                    }
                )

            for task in sort_tasks_for_manager(task_entries):
                items.append({"value": str(task["_template_path"]), "label": str(task["name"])})

        return items

    def _fallback_template_label(self, path: str, template_value: str, script_info: Dict[str, Any]) -> str:
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
        label = next(
            (item["label"] for item in self.list_templates() if item["value"] == path or item["value"] == template_value),
            self._fallback_template_label(path, template_value, script_info),
        )
        workspace_kind = normalize_workspace_kind(script_info.get("workspace_kind"))
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
        with open(path, "rb") as handle:
            raw = handle.read(MAX_TASK_PAYLOAD_BYTES + 1)
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
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise ValueError(f"Could not load template '{template_value}': {exc}") from exc

    @property
    def task_generator(self) -> TaskGenerator:
        with self._lock:
            if self._task_generator is None:
                self._task_generator = self._task_generator_factory(self.tasks_dir)
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

    def invalidate_cache(self) -> None:
        """Reset the full refresh rate-limiting timer to force a sync on next read."""
        with self._lock:
            self._last_full_refresh_time = 0.0

    @_with_stable_workspace
    def ensure_tasks_loaded(self, *, full_refresh: bool = False) -> None:
        """Load task metadata on demand for faster startup."""
        manager = self.task_manager
        if not self._tasks_loaded:
            if not manager.tasks:
                manager.scan_disk()
            manager.refresh_from_disk(force_all=True)
            self._tasks_loaded = True
            with self._lock:
                self._last_full_refresh_time = time.time()
            return
        if full_refresh:
            now = time.time()
            with self._lock:
                elapsed = now - self._last_full_refresh_time
            if elapsed >= 4.0:
                manager.refresh_from_disk(check_all=True, discover=True)
                with self._lock:
                    self._last_full_refresh_time = now

    @_with_stable_workspace
    def get_task_event_stream_context(self) -> tuple[str, TaskManager]:
        """Capture one atomic workspace/manager pair for a task event stream."""
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
    def get_task_log_stream_context(self, task_name: str) -> tuple[str, Dict[str, Any]]:
        """Capture one atomic workspace/task pair for a log stream."""
        task = self.get_task(task_name, refresh=False)
        if task is None:
            raise KeyError(task_name)
        return os.path.normcase(os.path.abspath(self.root_dir)), task

    @_with_stable_workspace
    def list_tasks(
        self,
        *,
        query: str = "",
        status: str = "All",
        offset: int = 0,
        limit: int = 50,
        refresh: bool = True,
        summary: bool = False,
        sort_mode: str = "priority",
    ) -> TaskPage:
        """Return tasks in the same logical order as the Manager page."""
        self.ensure_tasks_loaded(full_refresh=refresh)
        all_tasks = self.task_manager.list_tasks(summary=summary)
        status_counts = {
            "pending": 0,
            "queued": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
        }
        for task in all_tasks:
            task_status = str(task.get("status", "pending") or "pending").lower()
            status_counts[task_status] = status_counts.get(task_status, 0) + 1
        tasks = filter_tasks(all_tasks, query, status)
        ordered = sort_tasks_for_manager(tasks, sort_mode)

        safe_offset = max(0, int(offset))
        safe_limit = max(0, int(limit))
        items = ordered[safe_offset:] if safe_limit == 0 else ordered[safe_offset:safe_offset + safe_limit]
        if summary:
            items = _cap_summary_task_payloads(items)
        total = len(ordered)
        has_more = (safe_offset + len(items)) < total
        return TaskPage(
            items=items,
            total=total,
            offset=safe_offset,
            limit=safe_limit,
            has_more=has_more,
            status_counts=status_counts,
        )

    @_with_stable_workspace
    def get_dashboard(self, *, refresh: bool = True, recent_limit: int = 6) -> Dict[str, Any]:
        """Return lightweight dashboard data for the Home page."""

        page = self.list_tasks(limit=max(1, recent_limit), refresh=refresh, summary=True)
        all_tasks = self.task_manager.list_tasks(summary=True) if self._tasks_loaded else []
        summary = {
            "total": len(all_tasks),
            "running": 0,
            "queued": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
            "pending": 0,
        }
        for task in all_tasks:
            status = str(task.get("status", "pending")).lower()
            if status in summary:
                summary[status] += 1
        active_task = next(
            (
                task
                for task in page.items
                if str(task.get("status", "")).lower() in {"running", "queued"}
            ),
            page.items[0] if page.items else None,
        )
        return {
            "workspace": self.get_workspace_info(),
            "summary": summary,
            "recent_tasks": page.items,
            "template_count": len(self.list_templates()),
            "active_task": active_task,
        }

    @_with_stable_workspace
    def get_task(self, task_name: str, *, refresh: bool = True) -> Dict[str, Any] | None:
        """Return one task snapshot."""
        self.ensure_tasks_loaded(full_refresh=False)
        if refresh:
            self.task_manager.refresh_from_disk(task_ids=[task_name], check_all=True)
        task = self.task_manager.get_task(task_name)
        if task is None and refresh:
            self.task_manager.load_task_by_name(task_name)
            task = self.task_manager.get_task(task_name)
        return task

    def require_task(self, task_name: str, *, refresh: bool = True) -> Dict[str, Any]:
        """Return one task or raise ``KeyError``."""
        task = self.get_task(task_name, refresh=refresh)
        if task is None:
            raise KeyError(task_name)
        return task

    @_with_stable_workspace
    def start_task(self, task_name: str) -> Dict[str, Any]:
        """Start one task and return the updated snapshot."""
        task = self.require_task(task_name)
        if task.get("_load_error"):
            raise ValueError(str(task["_load_error"]))
        self.invalidate_cache()
        started = self.task_manager.start_task_now(task_name)
        if not started:
            raise ValueError(f"Task '{task_name}' could not be started")
        return self.get_task(task_name) or task

    @_with_stable_workspace
    def cancel_task(self, task_name: str) -> Dict[str, Any]:
        """Request cancellation from the runner that owns the task."""
        task = self.require_task(task_name)
        task_dir = str(task.get("dir", "") or "")
        info = load_task_info(task_dir) if task_dir else {}
        status = str(info.get("status", "") or "").lower()
        if status not in {"queued", "running"}:
            raise ValueError(f"Task '{task_name}' cannot be cancelled")
        self.invalidate_cache()
        ok = self.task_manager.request_task_cancel(
            task_name,
            expected_runner_id=str(info.get("runner_id", "") or ""),
            expected_run_index=active_task_run_index(info),
        )
        if not ok:
            raise ValueError(f"Task '{task_name}' cannot be cancelled")
        return self.get_task(task_name) or task

    @_with_stable_workspace
    def start_tasks_batch(
        self,
        task_names: List[str],
        *,
        max_workers: int | None = None,
    ) -> Dict[str, Any]:
        """Queue multiple tasks and return their updated snapshots."""
        normalized_names: List[str] = []
        seen: set[str] = set()
        for name in task_names:
            task_name = str(name or "").strip()
            if not task_name or task_name in seen:
                continue
            task = self.require_task(task_name, refresh=False)
            if task.get("_load_error"):
                raise ValueError(str(task["_load_error"]))
            normalized_names.append(task_name)
            seen.add(task_name)

        if not normalized_names:
            raise ValueError("No valid tasks were provided for batch run.")

        self.invalidate_cache()
        claimed_names = self.task_manager.start_batch_tasks(
            normalized_names,
            max_workers=max_workers,
        )
        if not claimed_names:
            raise ValueError("None of the selected tasks could be started.")
        claimed = set(claimed_names)
        items = [
            self.get_task(task_name, refresh=True) or self.require_task(task_name, refresh=False)
            for task_name in claimed_names
        ]
        return {
            "count": len(items),
            "items": items,
            "skipped": [task_name for task_name in normalized_names if task_name not in claimed],
        }

    @_with_stable_workspace
    def delete_tasks_batch(self, task_names: List[str]) -> Dict[str, Any]:
        """Soft-delete multiple tasks."""
        normalized_names: List[str] = []
        seen: set[str] = set()
        for name in task_names:
            task_name = str(name or "").strip()
            if not task_name or task_name in seen:
                continue
            self.require_task(task_name, refresh=False)
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

    @_with_stable_workspace
    def export_tasks_csv(self, task_names: List[str]) -> str:
        """Build a CSV export for the selected tasks."""

        normalized_names: List[str] = []
        seen: set[str] = set()
        tasks: List[Dict[str, Any]] = []
        for name in task_names:
            task_name = str(name or "").strip()
            if not task_name or task_name in seen:
                continue
            task = self.require_task(task_name, refresh=True)
            normalized_names.append(task_name)
            tasks.append(task)
            seen.add(task_name)

        if not normalized_names:
            raise ValueError("No valid tasks were provided for export.")

        csv_text = build_export_csv(tasks)
        if not csv_text:
            raise ValueError("No monitor data available to export.")
        return csv_text

    @_with_stable_workspace
    def set_task_pin(self, task_name: str, pinned: bool | None = None) -> Dict[str, Any]:
        """Toggle or set one task's pinned state."""
        self.require_task(task_name, refresh=False)
        self.invalidate_cache()
        ok, result = self.task_manager.set_task_pinned(task_name, pinned)
        if not ok:
            if str(result) == "Task not found":
                raise KeyError(task_name)
            raise ValueError(str(result))
        return self.require_task(task_name, refresh=True)

    @_with_stable_workspace
    def reorder_tasks(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Persist manual card order for the Manager page."""
        self.ensure_tasks_loaded(full_refresh=False)
        self.invalidate_cache()
        ok, result = self.task_manager.reorder_tasks(items)
        if not ok:
            message = str(result)
            if message.startswith("Task not found: "):
                raise KeyError(message.split(": ", 1)[1])
            raise ValueError(message)
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
            workspace = self.get_workspace_info()
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
    ) -> Dict[str, Any]:
        """Persist notes for one task."""
        self.require_task(task_name, refresh=False)
        self.invalidate_cache()
        try:
            ok, result = self.task_manager.update_task_notes(task_name, notes, expected_notes)
        except TaskStateConflict as exc:
            raise TaskNotesConflictError(str(exc)) from exc
        if not ok:
            if str(result) == "Task not found":
                raise KeyError(task_name)
            raise ValueError(str(result))
        return self.require_task(task_name, refresh=True)

    @_with_stable_workspace
    def update_task_env(
        self,
        task_name: str,
        env: Dict[str, Any],
        expected_env: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Persist env vars for one task."""
        self.require_task(task_name, refresh=False)
        self.invalidate_cache()
        try:
            ok, result = self.task_manager.update_task_env(task_name, env, expected_env)
        except TaskStateConflict as exc:
            raise TaskEnvConflictError(str(exc)) from exc
        if not ok:
            if str(result) == "Task not found":
                raise KeyError(task_name)
            raise ValueError(str(result))
        return self.require_task(task_name, refresh=True)

    @_with_stable_workspace
    def rename_task(self, task_name: str, new_name: str) -> Dict[str, Any]:
        """Rename one task."""
        self.require_task(task_name, refresh=False)
        self.invalidate_cache()
        ok, result = self.task_manager.rename_task(task_name, new_name)
        if not ok:
            message = str(result)
            if message == "Task not found":
                raise KeyError(task_name)
            raise ValueError(message)
        renamed = str(result)
        return self.require_task(renamed, refresh=True)

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
        chunk_size: int | None = None,
        expected_workspace_root: str | None = None,
        expected_task_dir: str | None = None,
    ) -> Dict[str, Any]:
        """Load a historical log chunk for the monitor page."""
        if expected_workspace_root is not None:
            current_root = os.path.normcase(os.path.abspath(self.root_dir))
            expected_root = os.path.normcase(os.path.abspath(expected_workspace_root))
            if current_root != expected_root:
                raise WorkspaceChangedError("Workspace changed")
        task = self.get_task(task_name, refresh=False)
        if task is None:
            raise KeyError(task_name)

        task_dir = str(task["dir"])
        if expected_task_dir is not None:
            current_task_dir = os.path.normcase(os.path.abspath(task_dir))
            expected_dir = os.path.normcase(os.path.abspath(expected_task_dir))
            if current_task_dir != expected_dir:
                raise WorkspaceChangedError("Workspace changed")
        log_options = get_log_options(task_dir)
        available_logs = list(log_options.keys())
        selected_name = str(log_file_name or "").strip()
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
            selected_path = resolve_log_path(task_dir)
            selected_name = os.path.basename(selected_path) if selected_path else ""

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
                "tail_limit_bytes": MAX_MONITOR_LOG_TAIL_BYTES,
            }

        tail_truncated = False
        tail_limit_bytes = MAX_MONITOR_LOG_TAIL_BYTES
        selected_identity = log_file_identity(selected_path)
        reset = False
        if offset is None:
            if tail_lines is not None:
                requested_tail_bytes = (
                    MAX_MONITOR_LOG_TAIL_BYTES
                    if tail_bytes is None
                    else max(1, int(tail_bytes))
                )
                tail_limit_bytes = min(MAX_MONITOR_LOG_TAIL_BYTES, requested_tail_bytes)
                content, new_offset = read_last_lines(
                    selected_path,
                    max_lines=max(0, tail_lines),
                    max_bytes=tail_limit_bytes,
                )
                if tail_lines > 0:
                    try:
                        tail_truncated = os.path.getsize(selected_path) > tail_limit_bytes
                    except OSError:
                        tail_truncated = False
            else:
                byte_limit = tail_bytes
                if byte_limit is None:
                    byte_limit = _int_setting(
                        self.settings,
                        "monitor_chunk_size",
                        _cfg.DEFAULT_MONITOR_CHUNK_SIZE,
                    )
                tail_limit_bytes = min(MAX_MONITOR_LOG_TAIL_BYTES, max(1, int(byte_limit)))
                content, new_offset = read_last_bytes(selected_path, n_bytes=tail_limit_bytes)
                try:
                    tail_truncated = os.path.getsize(selected_path) > tail_limit_bytes
                except OSError:
                    tail_truncated = False
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
                selected_size = 0
            identity_changed = bool(
                log_identity
                and selected_identity
                and str(log_identity) != selected_identity
            )
            reset = identity_changed or requested_offset > selected_size
            read_offset = 0 if reset else requested_offset
            content, new_offset = safe_read_log(selected_path, read_offset, max_bytes=max(1, byte_limit))

        return {
            "task_name": task_name,
            "selected_log": selected_name,
            "available_logs": available_logs,
            "content": content,
            "offset": new_offset,
            "reset": reset,
            "log_identity": selected_identity,
            "tail_truncated": tail_truncated,
            "tail_limit_bytes": tail_limit_bytes,
        }

    def get_metrics(self, *, include_processes: bool = False) -> Dict[str, Any]:
        """Return metrics, optionally including the more expensive GPU process list."""
        try:
            return self.metrics_sampler.sample(include_processes=include_processes)
        except TypeError:
            return self.metrics_sampler.sample()

    @_with_stable_workspace
    def open_shell_workspace(self) -> Dict[str, Any]:
        """Prepare and activate the project-level shell workspace."""

        current_root = self.root_dir or os.getenv(_cfg.ENV_KEY_ROOT, _cfg.ROOT_DIR)
        shell_root = bootstrap_shell_workspace(current_root)
        self.reload(shell_root)
        return self.get_workspace_info()

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
    ) -> Dict[str, Any]:
        """Create one or many tasks from script-yaml or shell input."""

        normalized_prefix = str(name_prefix or "").strip() or "task"
        if append_timestamp:
            normalized_prefix = f"{normalized_prefix}_{get_now_str()}"

        err = validate_task_name(normalized_prefix, self.tasks_dir)
        if err:
            raise ValueError(err)

        workspace_kind = self.get_workspace_info().get("workspace_kind", _cfg.WORKSPACE_KIND_SCRIPT)
        editor_mode = str(mode or "form").strip().lower()

        if workspace_kind == _cfg.WORKSPACE_KIND_SHELL:
            if editor_mode != "shell":
                raise ValueError("Shell workspace only supports shell mode.")
            content = _require_bounded_editor_text(shell_text, label="Shell content")
            if not content.strip():
                raise ValueError("Shell mode requires non-empty script content.")
            task = self.task_generator.create_shell_task(normalized_prefix, content)
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

        tasks = self.task_generator.create_tasks(
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

        workspace_kind = self.get_workspace_info().get("workspace_kind", _cfg.WORKSPACE_KIND_SCRIPT)
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

        preview_items: List[Dict[str, Any]] = []
        sample_configs = configs[: min(6, len(configs))]
        for index, config in enumerate(sample_configs, start=1):
            preview_items.append(
                {
                    "index": index,
                    "preview": preview_config_line(config),
                    "config": to_container(config, resolve=False),
                }
            )

        return {
            "count": len(configs),
            "items": preview_items,
            "task_kind": TASK_KIND_CONFIG,
        }

    def list_launcher_scripts(self) -> List[Dict[str, Any]]:
        """Return launchable scripts from the current project directory."""
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
    ) -> Dict[str, Any]:
        """Prepare and activate a workspace selected in the launcher."""

        workspace = bootstrap_workspace(script_path, config_path or None)
        self.reload(workspace)
        return self.get_workspace_info()

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

        current_script = self.get_workspace_info().get("script_path", "")
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

    def pick_and_open_launcher_workspace(self) -> Dict[str, Any]:
        """Open a native script picker and activate the chosen workspace."""

        if not native_picker_available():
            raise ValueError("Native file picker is unavailable on this server. Enter the path manually.")

        current_script = self.get_workspace_info().get("script_path", "")
        initial_dir = os.path.dirname(str(current_script or "")) or os.getcwd()
        script_path = choose_script_file(initial_dir)
        if not script_path:
            raise ValueError("No script selected.")
        return self.open_launcher_workspace(script_path)

    def pick_and_open_shell_workspace(self) -> Dict[str, Any]:
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

        return self.open_shell_workspace_at(selected_dir)

    @_with_stable_workspace
    def open_shell_workspace_at(self, project_dir: str) -> Dict[str, Any]:
        """Activate a shell workspace for a user-entered project directory."""

        selected_dir = normalize_path(project_dir)
        if not os.path.isdir(selected_dir):
            raise ValueError(f"Shell folder does not exist: {project_dir}")

        project_root = normalize_path(os.path.join(selected_dir, _cfg.DEFAULT_ROOT_NAME))
        workspace = bootstrap_shell_workspace(project_root)
        self.reload(workspace)
        return self.get_workspace_info()
