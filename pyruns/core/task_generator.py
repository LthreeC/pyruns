"""Task creation helpers for turning configs into disk-backed tasks."""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List

from pyruns._config import CONFIG_FILENAME, RUN_LOGS_DIR
from pyruns.utils import get_logger, get_now_str
from pyruns.utils.config_utils import save_yaml
from pyruns.utils.info_io import load_script_info, save_task_info

logger = get_logger(__name__)


def create_task_object(task_dir: str, name: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Build the in-memory representation used by TaskManager and the UI."""
    return {
        "dir": task_dir,
        "name": name,
        "status": "pending",
        "config": config,
        "log": "",
        "progress": 0.0,
        "created_at": get_now_str(),
        "env": {},
        "pinned": False,
        "start_times": [],
        "finish_times": [],
        "pids": [],
        "records": 0,
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
        """Remove UI-only metadata before persisting config.yaml."""
        return {
            key: value
            for key, value in config.items()
            if not str(key).startswith("_meta")
        }

    def create_task(
        self,
        name_prefix: str,
        config: Dict[str, Any],
        group_index: str = "",
        run_mode: str = "config",
    ) -> Dict[str, Any]:
        """Create one task folder with task_info.json, config.yaml, and run_logs/."""
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
        clean_config = self._clean_task_config(config)
        mode = str(run_mode or "config").strip().lower()

        task_obj = create_task_object(task_dir, display_name, clean_config)
        task_obj["run_mode"] = mode

        task_info: Dict[str, Any] = {
            "name": task_obj["name"],
            "status": task_obj["status"],
            "progress": task_obj["progress"],
            "created_at": task_obj["created_at"],
            "pinned": task_obj["pinned"],
            "run_mode": mode,
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
        save_yaml(os.path.join(task_dir, CONFIG_FILENAME), clean_config)
        os.makedirs(os.path.join(task_dir, RUN_LOGS_DIR), exist_ok=True)

        logger.debug("Created task '%s' at %s", display_name, task_dir)
        return task_obj

    def create_tasks(
        self,
        configs: List[Dict[str, Any]],
        name_prefix: str,
        run_mode: str = "config",
    ) -> List[Dict[str, Any]]:
        """Create many tasks, appending [i-of-n] suffixes when needed."""
        total = len(configs)
        tasks: List[Dict[str, Any]] = []
        for index, config in enumerate(configs, start=1):
            group_index = f"[{index}-of-{total}]" if total > 1 else ""
            tasks.append(
                self.create_task(
                    name_prefix,
                    config,
                    group_index=group_index,
                    run_mode=run_mode,
                )
            )
        return tasks
