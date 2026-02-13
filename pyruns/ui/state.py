import pyruns._config as _cfg
from dataclasses import dataclass, field
from typing import Dict, Any, List


@dataclass
class AppState:
    active_tab: str = "generator"
    # Read ROOT_DIR at instantiation time (not import time)
    # so it picks up the value patched by app.main()
    tasks_dir: str = field(default_factory=lambda: _cfg.ROOT_DIR)
    config_data: Dict[str, Any] = field(default_factory=dict)
    config_path: str = ""

    # Generator Settings
    task_name_input: str = ""

    # Manager Settings
    max_workers: int = 1
    execution_mode: str = "thread"
    selected_task_ids: List[str] = field(default_factory=list)
    manager_columns: int = 5
