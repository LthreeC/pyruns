import os
from nicegui import ui
from dataclasses import asdict

from pyruns.ui.state import AppState
from pyruns.core.task_generator import TaskGenerator
from pyruns.core.task_manager import TaskManager
from pyruns.core.system_metrics import SystemMonitor
from pyruns.ui.layout import render_main_layout
from pyruns.ui.pages.generator import render_generator_page
from pyruns.ui.pages.manager import render_manager_page
from pyruns.ui.pages.monitor import render_monitor_page

# Global Singletons (initialized lazily in main() to pick up fresh env vars)
task_generator: TaskGenerator = None  # type: ignore
task_manager: TaskManager = None  # type: ignore
metrics_sampler: SystemMonitor = None  # type: ignore

@ui.page("/")
def main_page():
    # Per-session state
    state = asdict(AppState())  # Convert to dict for mutability

    def page_router():
        if state["active_tab"] == "generator":
            render_generator_page(state, task_generator, task_manager)
        elif state["active_tab"] == "manager":
            render_manager_page(state, task_manager)
        elif state["active_tab"] == "monitor":
            render_monitor_page(state, task_manager)

    render_main_layout(state, task_manager, metrics_sampler, page_router)

def main():
    global task_generator, task_manager, metrics_sampler

    # Re-read ROOT_DIR from env at runtime.
    # cli.py sets PYRUNS_ROOT *after* _config.py was first imported,
    # so the module-level ROOT_DIR may be stale. Fix it here.
    import pyruns._config as _cfg
    fresh_root = os.getenv(_cfg.ENV_ROOT, _cfg.ROOT_DIR)
    _cfg.ROOT_DIR = fresh_root  # patch module-level constant for downstream use

    task_generator = TaskGenerator(root_dir=fresh_root)
    task_manager = TaskManager(root_dir=fresh_root)
    metrics_sampler = SystemMonitor()

    ui.run(title="Pyruns Experiment Lab", port=8080, show=True, reload=False, favicon="🧪")

if __name__ in {"__main__", "__mp_main__"}:
    main()
