import os
from dataclasses import dataclass, field
from typing import Dict, Any, List

from pyruns._config import ROOT_DIR

@dataclass
class AppState:
    active_tab: str = "generator"
    tasks_dir: str = ROOT_DIR
    config_data: Dict[str, Any] = field(default_factory=dict)
    config_path: str = ""
    
    # Generator Settings
    task_name_input: str = ""
    
    # Manager Settings
    max_workers: int = 1
    execution_mode: str = "thread"
    selected_task_ids: List[str] = field(default_factory=list)
    manager_columns: int = 4
    
    # System (metrics refresh is fixed at 3s in header.py)
