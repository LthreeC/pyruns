"""Task creation helpers for turning configs or shell scripts into disk-backed tasks."""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List

from pyruns._config import (
    RUN_LOGS_DIR,
    TASK_KIND_CONFIG,
    TASK_KIND_SHELL,
    TASK_KIND_TO_CONFIG_FILENAME,
)
from pyruns.utils import get_logger, get_now_str
from pyruns.utils.info_io import load_script_info, save_task_info
from pyruns.utils.task_files import (
    build_task_preview_and_search,
    normalize_task_kind,
    write_task_payload,
)

logger = get_logger(__name__)


def _resolve_requested_task_kind(task_kind: str) -> str:
    """Normalize task-kind input and reject unsupported values."""

    requested_kind = str(task_kind or TASK_KIND_CONFIG).strip().lower()
    normalized_kind = normalize_task_kind(requested_kind)
    if normalized_kind != requested_kind:
        raise ValueError(f"Unsupported task kind: {task_kind}")
    return normalized_kind


def create_task_object(
    task_dir: str,
    name: str,
    *,
    task_kind: str = TASK_KIND_CONFIG,
    config: Dict[str, Any] | None = None,
    config_text: str = "",
    config_file: str | None = None,
) -> Dict[str, Any]:
    """Build the in-memory representation used by TaskManager and the UI."""

    normalized_kind = _resolve_requested_task_kind(task_kind)
    resolved_config_file = config_file or TASK_KIND_TO_CONFIG_FILENAME[normalized_kind]
    preview_text, search_text = build_task_preview_and_search(
        task_kind=normalized_kind,
        config=config or {},
        config_text=config_text,
        task_name=name,
    )
    return {
        "dir": task_dir,
        "name": name,
        "status": "pending",
        "config": config or {},
        "config_text": config_text if normalized_kind == TASK_KIND_SHELL else "",
        "config_file": resolved_config_file,
        "task_kind": normalized_kind,
        "log": "",
        "progress": 0.0,
        "created_at": get_now_str(),
        "env": {},
        "pinned": False,
        "start_times": [],
        "finish_times": [],
        "pids": [],
        "records": 0,
        "tracks": 0,
        "notes": "",
        "preview_text": preview_text,
        "search_text": search_text,
    }


class TaskGenerator:
    """Create one or many task folders under the workspace tasks directory."""

    def __init__(self, root_dir: str | None = None):
        if root_dir is None:
            from pyruns._config import ROOT_DIR, TASKS_DIR

            root_dir = os.path.join(ROOT_DIR, TASKS_DIR)

        self.root_dir = root_dir
        os.makedirs(self.root_dir, exist_ok=True)

    def _resolve_script_path(self) -> str:
        """Best-effort lookup of the source script from workspace metadata."""

        workspace_dir = os.path.dirname(self.root_dir)
        script_info = load_script_info(workspace_dir)
        script_path = str(script_info.get("script_path", "") or "")
        return script_path if script_path and os.path.exists(script_path) else ""

    @staticmethod
    def _clean_task_config(config: Dict[str, Any]) -> Dict[str, Any]:
        """Remove UI-only metadata before persisting ``config.yaml``."""

        return {
            key: value
            for key, value in (config or {}).items()
            if not str(key).startswith("_meta")
        }

    def create_task(
        self,
        name_prefix: str,
        config: Dict[str, Any] | None = None,
        *,
        config_text: str = "",
        group_index: str = "",
        task_kind: str = TASK_KIND_CONFIG,
    ) -> Dict[str, Any]:
        """Create one task folder with task metadata, task payload, and ``run_logs/``."""

        timestamp = get_now_str()
        base_name = name_prefix.strip() if name_prefix else ""
        if not base_name:
            base_name = f"task_{timestamp}"

        folder_name = f"{base_name}_{group_index}" if group_index else base_name
        if os.path.exists(os.path.join(self.root_dir, folder_name)):
            folder_name = f"{folder_name}_{int(time.time() * 1000)}"

        task_dir = os.path.join(self.root_dir, folder_name)
        os.makedirs(task_dir, exist_ok=True)

        display_name = f"{base_name}_{group_index}" if group_index else base_name
        normalized_kind = _resolve_requested_task_kind(task_kind)
        resolved_config_file = TASK_KIND_TO_CONFIG_FILENAME[normalized_kind]
        clean_config = self._clean_task_config(config or {})
        clean_config_text = str(config_text or "")

        task_obj = create_task_object(
            task_dir,
            display_name,
            task_kind=normalized_kind,
            config=clean_config,
            config_text=clean_config_text,
            config_file=resolved_config_file,
        )

        task_info: Dict[str, Any] = {
            "name": task_obj["name"],
            "status": task_obj["status"],
            "progress": task_obj["progress"],
            "created_at": task_obj["created_at"],
            "pinned": task_obj["pinned"],
            "task_kind": normalized_kind,
            "config_file": resolved_config_file,
            "start_times": [],
            "finish_times": [],
            "pids": [],
            "records": [],
            "tracks": [],
        }

        script_path = self._resolve_script_path()
        if script_path:
            task_info["script"] = script_path

        save_task_info(task_dir, task_info)
        write_task_payload(
            task_dir,
            task_kind=normalized_kind,
            config_file=resolved_config_file,
            config=clean_config,
            config_text=clean_config_text,
        )
        os.makedirs(os.path.join(task_dir, RUN_LOGS_DIR), exist_ok=True)

        logger.debug("Created task '%s' at %s", display_name, task_dir)
        return task_obj

    def create_tasks(
        self,
        configs: List[Dict[str, Any]],
        name_prefix: str,
        task_kind: str = TASK_KIND_CONFIG,
    ) -> List[Dict[str, Any]]:
        """Create many config tasks, appending ``[i-of-n]`` suffixes when needed."""

        total = len(configs)
        normalized_kind = _resolve_requested_task_kind(task_kind)
        tasks: List[Dict[str, Any]] = []
        for index, config in enumerate(configs, start=1):
            group_index = f"[{index}-of-{total}]" if total > 1 else ""
            tasks.append(
                self.create_task(
                    name_prefix,
                    config,
                    group_index=group_index,
                    task_kind=normalized_kind,
                )
            )
        return tasks

    def create_shell_task(
        self,
        name_prefix: str,
        shell_text: str,
    ) -> Dict[str, Any]:
        """Create a single shell task backed by ``config.sh``."""

        return self.create_task(
            name_prefix,
            config=None,
            config_text=shell_text,
            task_kind=TASK_KIND_SHELL,
        )
