import os
import time
import json
import datetime
from typing import Dict, Any, List

from pyruns._config import ROOT_DIR, INFO_FILENAME, CONFIG_FILENAME, LOG_FILENAME, ENV_SCRIPT
from pyruns.utils.config_utils import save_yaml
from pyruns.utils.task_utils import validate_task_name  # re-export for backward compat
from pyruns.utils import get_logger

logger = get_logger(__name__)


def create_task_object(task_id: str, task_dir: str, name: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Create the in-memory dictionary representing a task."""
    created_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
        "run_at": None,
        "rerun_at": [],
        "run_pid": None,
        "rerun_pid": [],
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
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

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

        task_id = f"{ts}_{int(time.time() * 1000)}"

        # Display name = folder name (readable)
        display_name = base_name
        if group_index:
            display_name += f"-{group_index}"

        # Clean config: remove internal _meta keys before saving
        clean_config = {k: v for k, v in config.items() if not k.startswith("_meta")}

        task_obj = create_task_object(task_id, task_dir, display_name, clean_config)

        # ── Write files ──
        # task_info.json
        info = {k: v for k, v in task_obj.items() if k not in ["dir", "config", "log"]}
        env_script = os.environ.get(ENV_SCRIPT)
        if env_script:
            info["script"] = env_script
        with open(os.path.join(task_dir, INFO_FILENAME), "w", encoding="utf-8") as f:
            json.dump(info, f, indent=4, ensure_ascii=False)

        # config.yaml: only clean parameters
        save_yaml(os.path.join(task_dir, CONFIG_FILENAME), clean_config)

        # run.log: initial entry
        with open(os.path.join(task_dir, LOG_FILENAME), "w", encoding="utf-8") as f:
            f.write(f"[{info['created_at']}] Task initialized.\n")

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
