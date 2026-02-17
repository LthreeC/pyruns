import os
import time
import json
from typing import Dict, Any, List

from pyruns._config import ROOT_DIR, INFO_FILENAME, CONFIG_FILENAME, RUN_LOG_DIR, ENV_SCRIPT
from pyruns.utils.config_utils import save_yaml
from pyruns.utils.task_utils import validate_task_name  # re-export for backward compat
from pyruns.utils import get_logger, get_now_str, get_now_str_us

logger = get_logger(__name__)


def create_task_object(task_id: str, task_dir: str, name: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Create the in-memory dictionary representing a task."""
    created_at = get_now_str()
    return {
        "id": task_id,
        "dir": task_dir,
        "name": name,
        "status": "pending",
        "config": config,
        "log": "",
        "progress": 0.0,
        "created_at": created_at,
        "env": {},
        "pinned": False,
        "run_at": [],
        "run_pid": [],
        "monitor": [],
    }


class TaskGenerator:
    def __init__(self, root_dir: str = ROOT_DIR):
        self.root_dir = root_dir
        os.makedirs(self.root_dir, exist_ok=True)

    def create_task(
        self,
        name_prefix: str,
        config: Dict[str, Any],
        group_index: str = "",
    ) -> Dict[str, Any]:
        ts = get_now_str()

        # ── Folder name = user-provided task name ──
        base_name = name_prefix.strip() if name_prefix else ""
        if not base_name:
            base_name = f"task_{ts}"

        folder_name = base_name
        if group_index:
            folder_name += f"-{group_index}"  # e.g. "my-exp-[1-of-12]"

        # Deduplicate: append millis if folder already exists
        if os.path.exists(os.path.join(self.root_dir, folder_name)):
            folder_name += f"_{int(time.time() * 1000)}"

        task_dir = os.path.join(self.root_dir, folder_name)
        os.makedirs(task_dir, exist_ok=True)

        # Task ID — microsecond-level timestamp, prefixed for batch tasks
        ts_us = get_now_str_us()
        if group_index:
            task_id = f"{group_index}_{ts_us}"
        else:
            task_id = ts_us

        # Display name = folder name (readable)
        display_name = base_name
        if group_index:
            display_name += f"-{group_index}"

        # Clean config: remove internal _meta keys before saving
        clean_config = {k: v for k, v in config.items() if not k.startswith("_meta")}

        task_obj = create_task_object(task_id, task_dir, display_name, clean_config)

        # ── Write files ──
        # task_info.json — ordered fields
        info = {
            "id": task_obj["id"],
            "name": task_obj["name"],
            "status": task_obj["status"],
            "progress": task_obj["progress"],
            "created_at": task_obj["created_at"],
            "pinned": task_obj["pinned"],
        }
        env_script = os.environ.get(ENV_SCRIPT)
        if env_script:
            info["script"] = env_script
        # Array fields at the end
        info["start_times"] = []
        info["finish_times"] = []
        info["pids"] = []
        info["monitors"] = []

        with open(os.path.join(task_dir, INFO_FILENAME), "w", encoding="utf-8") as f:
            json.dump(info, f, indent=4, ensure_ascii=False)

        # config.yaml: only clean parameters
        save_yaml(os.path.join(task_dir, CONFIG_FILENAME), clean_config)

        # Create run_logs/ directory (empty, logs created on first run)
        os.makedirs(os.path.join(task_dir, RUN_LOG_DIR), exist_ok=True)

        logger.debug("Created task '%s' at %s", display_name, task_dir)
        return task_obj

    def create_tasks(
        self,
        configs: List[Dict[str, Any]],
        name_prefix: str,
    ) -> List[Dict[str, Any]]:
        tasks: List[Dict[str, Any]] = []
        total = len(configs)
        for idx, cfg in enumerate(configs):
            group_index = f"[{idx + 1}-of-{total}]" if total > 1 else ""
            tasks.append(self.create_task(name_prefix, cfg, group_index))
        return tasks

