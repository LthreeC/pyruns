"""
Per-session application state — initialised from workspace settings.
"""
import pyruns._config as _cfg
from dataclasses import dataclass, field
from typing import Dict, Any, List


@dataclass
class AppState:
    """Mutable per-session state dict (converted via ``asdict()``).

    *_settings* is an optional dict from ``_pyruns_.yaml`` that overrides
    the hard-coded defaults below.
    """
    _settings: Dict[str, Any] = field(default_factory=dict, repr=False)

    active_tab: str = "generator"
    tasks_dir: str = field(default_factory=lambda: __import__('os').path.join(_cfg.ROOT_DIR, _cfg.TASKS_DIR))
    config_data: Dict[str, Any] = field(default_factory=dict)
    config_path: str = ""

    # Generator
    task_name_input: str = ""

    # Manager
    max_workers: int = 1
    execution_mode: str = "thread"
    selected_task_ids: List[str] = field(default_factory=list)
    manager_columns: int = 5

    def __post_init__(self):
        s = self._settings
        if not s:
            return
        # Override defaults from workspace settings
        self.manager_columns = int(s.get("manager_columns", self.manager_columns))
        self.max_workers = int(s.get("manager_max_workers", self.max_workers))
        self.execution_mode = str(s.get("manager_execution_mode", self.execution_mode))
