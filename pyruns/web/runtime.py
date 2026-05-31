"""Shared runtime state for the Pyruns API server."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

import yaml

import pyruns._config as _cfg
from pyruns._config import (
    CONFIG_DEFAULT_FILENAME,
    CONFIG_FILENAME,
    SCRIPT_INFO_FILENAME,
    SHELL_WORKSPACE_NAME,
    TASKS_DIR,
    TASK_KIND_CONFIG,
    TASK_KIND_SHELL,
)
from pyruns.core.system_metrics import SystemMonitor
from pyruns.core.task_generator import TaskGenerator
from pyruns.core.task_manager import TaskManager
from pyruns.core.report import build_export_csv
from pyruns.launcher import (
    bootstrap_shell_workspace,
    bootstrap_workspace,
    choose_directory,
    choose_script_file,
    get_config_selection_metadata,
    list_config_candidates,
    list_script_candidates,
    list_workspace_candidates,
    native_picker_available,
    normalize_path,
    shell_project_root_for_workspace,
    shell_workspace_root_for_run_root,
)
from pyruns.utils import get_now_str
from pyruns.utils.batch_utils import generate_batch_configs
from pyruns.utils.config_utils import (
    list_template_files,
    load_yaml_strict,
    preview_config_line,
    validate_config_types_against_template,
)
from pyruns.utils.info_io import get_log_options, load_script_info, resolve_log_path
from pyruns.utils.log_io import read_last_bytes, read_last_lines, safe_read_log
from pyruns.utils.settings import ensure_settings_file, load_settings
from pyruns.utils.shell_runtime import get_shell_runtime_for_workspace
from pyruns.utils.sort_utils import filter_tasks, task_sort_key
from pyruns.utils.info_io import validate_task_name
from pyruns.utils.task_files import build_task_preview_and_search, normalize_workspace_kind


TaskManagerFactory = Callable[[str], TaskManager]
TaskGeneratorFactory = Callable[[str], TaskGenerator]
MetricsFactory = Callable[[], SystemMonitor]


def _int_setting(settings: Dict[str, Any], key: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(settings.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


@dataclass
class TaskPage:
    """Paginated task list result for the API layer."""

    items: List[Dict[str, Any]]
    total: int
    offset: int
    limit: int
    has_more: bool


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

        self._lock = threading.RLock()
        self.root_dir = ""
        self.tasks_dir = ""
        self.settings: Dict[str, Any] = {}
        self._task_manager: TaskManager | None = None
        self._task_generator: TaskGenerator | None = None
        self._metrics_sampler: SystemMonitor | None = None
        self._tasks_loaded = False
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

    def reload(self, root_dir: str | None = None) -> None:
        """Reset runtime state for the active workspace without heavy I/O."""
        resolved_root = self._normalize_path(
            root_dir or os.getenv(_cfg.ENV_KEY_ROOT, _cfg.ROOT_DIR)
        )
        tasks_dir = self._normalize_path(os.path.join(resolved_root, TASKS_DIR))

        _cfg.ROOT_DIR = resolved_root
        os.environ[_cfg.ENV_KEY_ROOT] = resolved_root
        os.makedirs(tasks_dir, exist_ok=True)
        ensure_settings_file(resolved_root)

        with self._lock:
            self.root_dir = resolved_root
            self.tasks_dir = tasks_dir
            self.settings = load_settings(resolved_root)
            self._task_manager = None
            self._task_generator = None
            self._metrics_sampler = None
            self._tasks_loaded = False

    def change_run_root(self, new_root: str) -> Dict[str, Any]:
        """Switch the active workspace after validation."""
        resolved_root = self._normalize_path(new_root)
        if not self._has_workspace_markers(resolved_root):
            raise ValueError(
                f"Run Root must contain {TASKS_DIR}/, {CONFIG_DEFAULT_FILENAME}, or {SCRIPT_INFO_FILENAME}"
            )
        self.reload(resolved_root)
        return self.get_workspace_info()

    def get_workspace_info(self) -> Dict[str, Any]:
        """Return current workspace metadata for the frontend."""
        script_info = load_script_info(self.root_dir)
        workspace_kind = normalize_workspace_kind(script_info.get("workspace_kind"))
        workspace_ready = bool(script_info.get("script_name") or workspace_kind)
        script_path = str(script_info.get("script_path", "") or "")
        project_root = str(script_info.get("project_root", "") or "")
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
            "project_root": project_root,
            "workspace_kind": workspace_kind,
            "workspace_ready": workspace_ready,
            "settings": dict(self.settings),
            "shell_runtime": get_shell_runtime_for_workspace(self.root_dir),
            "templates": self.list_templates(),
        }

    def list_templates(self) -> List[Dict[str, str]]:
        """Return loadable template options for the Generator page."""
        script_info = load_script_info(self.root_dir)
        if normalize_workspace_kind(script_info.get("workspace_kind")) == _cfg.WORKSPACE_KIND_SHELL:
            return []
        options = list_template_files(self.root_dir)
        return [{"value": value, "label": label} for value, label in options.items()]

    def resolve_template_path(self, template_value: str) -> str:
        """Resolve one template entry from workspace-relative or absolute value."""

        value = str(template_value or "").strip()
        if not value:
            raise FileNotFoundError("Template value is empty.")

        if os.path.isabs(value):
            path = value
        else:
            path = os.path.join(self.root_dir, value)

        normalized = self._normalize_path(path)
        if not os.path.exists(normalized):
            raise FileNotFoundError(f"Template not found: {template_value}")
        return normalized

    def get_template_content(self, template_value: str) -> Dict[str, Any]:
        """Return the raw template text and metadata for one template."""

        path = self.resolve_template_path(template_value)
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()

        label = next(
            (item["label"] for item in self.list_templates() if item["value"] == template_value),
            os.path.basename(path),
        )
        script_info = load_script_info(self.root_dir)
        workspace_kind = normalize_workspace_kind(script_info.get("workspace_kind"))
        mode_hint = "shell" if (workspace_kind == _cfg.WORKSPACE_KIND_SHELL or path.endswith(".sh")) else "yaml"
        data: Any = None
        if mode_hint == "yaml":
            try:
                data = yaml.safe_load(content)
            except yaml.YAMLError:
                data = None

        return {
            "value": template_value,
            "label": label,
            "path": path,
            "content": content,
            "read_only": os.path.basename(path) == CONFIG_DEFAULT_FILENAME,
            "mode_hint": mode_hint,
            "parsed_config": data if isinstance(data, dict) else None,
        }

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
            return self._task_manager

    @property
    def metrics_sampler(self) -> SystemMonitor:
        with self._lock:
            if self._metrics_sampler is None:
                self._metrics_sampler = self._metrics_factory()
            return self._metrics_sampler

    def ensure_tasks_loaded(self, *, full_refresh: bool = False) -> None:
        """Load task metadata on demand for faster startup."""
        manager = self.task_manager
        if not self._tasks_loaded:
            if not manager.tasks:
                manager.scan_disk()
            manager.refresh_from_disk(force_all=True)
            self._tasks_loaded = True
            return
        if full_refresh:
            manager.refresh_from_disk(check_all=True)

    def list_tasks(
        self,
        *,
        query: str = "",
        status: str = "All",
        offset: int = 0,
        limit: int = 50,
        refresh: bool = True,
    ) -> TaskPage:
        """Return tasks in the same logical order as the Manager page."""
        self.ensure_tasks_loaded(full_refresh=refresh)
        tasks = filter_tasks(self.task_manager.list_tasks(), query, status)
        pinned = sorted(
            [task for task in tasks if task.get("pinned")],
            key=task_sort_key,
            reverse=True,
        )
        others = sorted(
            [task for task in tasks if not task.get("pinned")],
            key=task_sort_key,
            reverse=True,
        )
        ordered = pinned + others

        safe_offset = max(0, int(offset))
        safe_limit = max(0, int(limit))
        items = ordered[safe_offset:] if safe_limit == 0 else ordered[safe_offset:safe_offset + safe_limit]
        total = len(ordered)
        has_more = (safe_offset + len(items)) < total
        return TaskPage(items=items, total=total, offset=safe_offset, limit=safe_limit, has_more=has_more)

    def get_dashboard(self, *, refresh: bool = True, recent_limit: int = 6) -> Dict[str, Any]:
        """Return lightweight dashboard data for the Home page."""

        page = self.list_tasks(limit=max(1, recent_limit), refresh=refresh)
        all_tasks = self.task_manager.list_tasks() if self._tasks_loaded else []
        summary = {
            "total": len(all_tasks),
            "running": 0,
            "queued": 0,
            "completed": 0,
            "failed": 0,
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

    def get_task(self, task_name: str, *, refresh: bool = True) -> Dict[str, Any] | None:
        """Return one task snapshot."""
        self.ensure_tasks_loaded(full_refresh=False)
        if refresh:
            self.task_manager.refresh_from_disk(task_ids=[task_name], check_all=True)
        return self.task_manager.get_task(task_name)

    def require_task(self, task_name: str, *, refresh: bool = True) -> Dict[str, Any]:
        """Return one task or raise ``KeyError``."""
        task = self.get_task(task_name, refresh=refresh)
        if task is None:
            raise KeyError(task_name)
        return task

    def start_task(self, task_name: str, execution_mode: str | None = None) -> Dict[str, Any]:
        """Start one task and return the updated snapshot."""
        task = self.require_task(task_name)
        if task.get("_load_error"):
            raise ValueError(str(task["_load_error"]))
        self.task_manager.start_task_now(task_name, execution_mode)
        return self.get_task(task_name) or task

    def cancel_task(self, task_name: str) -> Dict[str, Any]:
        """Cancel one task and return the updated snapshot."""
        task = self.require_task(task_name)
        ok = self.task_manager.cancel_task(task_name)
        if not ok:
            raise ValueError(f"Task '{task_name}' cannot be cancelled")
        return self.get_task(task_name) or task

    def start_tasks_batch(
        self,
        task_names: List[str],
        *,
        execution_mode: str | None = None,
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

        self.task_manager.start_batch_tasks(
            normalized_names,
            execution_mode=execution_mode,
            max_workers=max_workers,
        )
        items = [
            self.get_task(task_name, refresh=True) or self.require_task(task_name, refresh=False)
            for task_name in normalized_names
        ]
        return {
            "count": len(items),
            "items": items,
        }

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

        self.task_manager.delete_tasks(normalized_names)
        return {
            "count": len(normalized_names),
            "deleted": normalized_names,
        }

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

    def set_task_pin(self, task_name: str, pinned: bool | None = None) -> Dict[str, Any]:
        """Toggle or set one task's pinned state."""
        self.require_task(task_name, refresh=False)
        ok, result = self.task_manager.set_task_pinned(task_name, pinned)
        if not ok:
            if str(result) == "Task not found":
                raise KeyError(task_name)
            raise ValueError(str(result))
        return self.require_task(task_name, refresh=True)

    def update_task_notes(self, task_name: str, notes: str) -> Dict[str, Any]:
        """Persist notes for one task."""
        self.require_task(task_name, refresh=False)
        ok, result = self.task_manager.update_task_notes(task_name, notes)
        if not ok:
            if str(result) == "Task not found":
                raise KeyError(task_name)
            raise ValueError(str(result))
        return self.require_task(task_name, refresh=True)

    def update_task_env(self, task_name: str, env: Dict[str, Any]) -> Dict[str, Any]:
        """Persist env vars for one task."""
        self.require_task(task_name, refresh=False)
        ok, result = self.task_manager.update_task_env(task_name, env)
        if not ok:
            if str(result) == "Task not found":
                raise KeyError(task_name)
            raise ValueError(str(result))
        return self.require_task(task_name, refresh=True)

    def rename_task(self, task_name: str, new_name: str) -> Dict[str, Any]:
        """Rename one task."""
        self.require_task(task_name, refresh=False)
        ok, result = self.task_manager.rename_task(task_name, new_name)
        if not ok:
            message = str(result)
            if message == "Task not found":
                raise KeyError(task_name)
            raise ValueError(message)
        renamed = str(result)
        return self.require_task(renamed, refresh=True)

    def get_task_logs(
        self,
        task_name: str,
        *,
        log_file_name: str | None = None,
        offset: int | None = None,
        tail_bytes: int | None = None,
        tail_lines: int | None = None,
        chunk_size: int | None = None,
    ) -> Dict[str, Any]:
        """Load a historical log chunk for the monitor page."""
        task = self.get_task(task_name, refresh=False)
        if task is None:
            raise KeyError(task_name)

        task_dir = str(task["dir"])
        selected_path = resolve_log_path(task_dir, log_file_name)
        available_logs = list(get_log_options(task_dir).keys())
        selected_name = os.path.basename(selected_path) if selected_path else ""

        if not selected_path:
            return {
                "task_name": task_name,
                "selected_log": selected_name,
                "available_logs": available_logs,
                "content": "",
                "offset": 0,
            }

        if offset is None:
            if tail_lines is not None:
                content, new_offset = read_last_lines(selected_path, max_lines=max(0, tail_lines))
            else:
                byte_limit = tail_bytes
                if byte_limit is None:
                    byte_limit = _int_setting(
                        self.settings,
                        "monitor_chunk_size",
                        _cfg.DEFAULT_MONITOR_CHUNK_SIZE,
                    )
                content, new_offset = read_last_bytes(selected_path, n_bytes=max(1, byte_limit))
        else:
            byte_limit = chunk_size
            if byte_limit is None:
                byte_limit = _int_setting(
                    self.settings,
                    "monitor_chunk_size",
                    _cfg.DEFAULT_MONITOR_CHUNK_SIZE,
                )
            content, new_offset = safe_read_log(selected_path, max(0, offset), max_bytes=max(1, byte_limit))

        return {
            "task_name": task_name,
            "selected_log": selected_name,
            "available_logs": available_logs,
            "content": content,
            "offset": new_offset,
        }

    def get_metrics(self) -> Dict[str, Any]:
        """Return a fresh system metrics snapshot."""
        return self.metrics_sampler.sample()

    def open_shell_workspace(self) -> Dict[str, Any]:
        """Prepare and activate the project-level shell workspace."""

        current_root = self.root_dir or os.getenv(_cfg.ENV_KEY_ROOT, _cfg.ROOT_DIR)
        shell_root = bootstrap_shell_workspace(current_root)
        self.reload(shell_root)
        return self.get_workspace_info()

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
            content = str(shell_text or "")
            if not content.strip():
                raise ValueError("Shell mode requires non-empty script content.")
            task = self.task_generator.create_shell_task(normalized_prefix, content)
            self.task_manager.add_task(task)
            page = self.list_tasks(limit=12, refresh=False)
            return {
                "count": 1,
                "items": [task],
                "recent_tasks": page.items,
                "task_kind": TASK_KIND_SHELL,
            }

        if editor_mode not in {"form", "yaml"}:
            raise ValueError(f"Unsupported generator mode: {mode}")

        try:
            parsed = yaml.safe_load(str(yaml_text or ""))
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML: {exc}") from exc

        if parsed is None:
            base_config = {}
        elif isinstance(parsed, dict):
            base_config = parsed
        else:
            raise ValueError("YAML content must be a mapping at the root.")

        if template_value:
            try:
                orig_config = load_yaml_strict(self.resolve_template_path(template_value))
            except Exception:
                orig_config = {}
        else:
            orig_config = {}

        if editor_mode == "form":
            try:
                configs = generate_batch_configs(base_config)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
        else:
            if generate_batch_configs(base_config) != [base_config]:
                raise ValueError("YAML mode does not support batch syntax. Switch back to Form mode.")
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
        self.task_manager.add_tasks(tasks)
        page = self.list_tasks(limit=12, refresh=False)
        return {
            "count": len(tasks),
            "items": [item for item in tasks],
            "recent_tasks": page.items,
            "task_kind": TASK_KIND_CONFIG,
        }

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
            content = str(shell_text or "")
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
            parsed = yaml.safe_load(str(yaml_text or ""))
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML: {exc}") from exc

        if parsed is None:
            base_config = {}
        elif isinstance(parsed, dict):
            base_config = parsed
        else:
            raise ValueError("YAML content must be a mapping at the root.")

        if template_value:
            try:
                orig_config = load_yaml_strict(self.resolve_template_path(template_value))
            except Exception:
                orig_config = {}
        else:
            orig_config = {}

        if editor_mode == "form":
            try:
                configs = generate_batch_configs(base_config)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
        else:
            if generate_batch_configs(base_config) != [base_config]:
                raise ValueError("YAML mode does not support batch syntax. Switch back to Form mode.")
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
                    "config": config,
                }
            )

        return {
            "count": len(configs),
            "items": preview_items,
            "task_kind": TASK_KIND_CONFIG,
        }

    def list_launcher_scripts(self) -> List[Dict[str, Any]]:
        """Return launchable scripts from the current project directory."""
        return list_script_candidates()

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

    def open_launcher_workspace(
        self,
        script_path: str,
        config_path: str | None = None,
    ) -> Dict[str, Any]:
        """Prepare and activate a workspace selected in the launcher."""

        workspace = bootstrap_workspace(script_path, config_path or None)
        self.reload(workspace)
        return self.get_workspace_info()

    def validate_launcher_path(self, kind: str, path: str) -> Dict[str, Any]:
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
            suffix = os.path.splitext(normalized)[1].lower()
            ok = os.path.isfile(normalized) and suffix in {".yaml", ".yml"}
            return {
                "ok": ok,
                "kind": "config",
                "normalized_path": normalized,
                "path_type": "file" if os.path.isfile(normalized) else "",
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

    def pick_and_open_launcher_workspace(self) -> Dict[str, Any]:
        """Open a native script picker and activate the chosen workspace."""

        if not native_picker_available():
            raise ValueError("Native file picker is unavailable on this server. Enter the path manually.")

        current_script = self.get_workspace_info().get("script_path", "")
        initial_dir = os.path.dirname(str(current_script or "")) or os.getcwd()
        script_path = choose_script_file(initial_dir)
        if not script_path:
            raise ValueError("No script selected.")
        workspace = bootstrap_workspace(script_path)
        self.reload(workspace)
        return self.get_workspace_info()

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

        project_root = normalize_path(os.path.join(selected_dir, _cfg.DEFAULT_ROOT_NAME))
        workspace = bootstrap_shell_workspace(project_root)
        self.reload(workspace)
        return self.get_workspace_info()

    def open_shell_workspace_at(self, project_dir: str) -> Dict[str, Any]:
        """Activate a shell workspace for a user-entered project directory."""

        selected_dir = normalize_path(project_dir)
        if not os.path.isdir(selected_dir):
            raise ValueError(f"Shell folder does not exist: {project_dir}")

        project_root = normalize_path(os.path.join(selected_dir, _cfg.DEFAULT_ROOT_NAME))
        workspace = bootstrap_shell_workspace(project_root)
        self.reload(workspace)
        return self.get_workspace_info()
