"""
Application entry point — initialises singletons and starts the NiceGUI server.
"""
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

# Global singletons (initialised lazily in main())
task_generator: TaskGenerator = None  # type: ignore
task_manager: TaskManager = None  # type: ignore
metrics_sampler: SystemMonitor = None  # type: ignore
_settings: dict = {}  # workspace settings from _pyruns_.yaml


@ui.page("/")
def main_page():
    """Per-session page — creates mutable state and routes to the active tab."""
    state = asdict(AppState(_settings=_settings))

    def page_router():
        tab = state["active_tab"]
        if tab == "generator":
            render_generator_page(state, task_generator, task_manager)
        elif tab == "manager":
            render_manager_page(state, task_manager)
        elif tab == "monitor":
            render_monitor_page(state, task_manager)

    render_main_layout(state, task_manager, metrics_sampler, page_router)


def main():
    """Bootstrap the app: read env vars, create singletons, start server."""
    global task_generator, task_manager, metrics_sampler, _settings

    # Re-read ROOT_DIR from env at runtime (cli.py may have set it after
    # _config.py was first imported).
    import pyruns._config as _cfg
    fresh_root = os.getenv(_cfg.ENV_ROOT, _cfg.ROOT_DIR)
    _cfg.ROOT_DIR = fresh_root

    # Load workspace settings
    from pyruns.utils.settings import load_settings, ensure_settings_file
    ensure_settings_file(fresh_root)
    _settings = load_settings(fresh_root)

    task_generator = TaskGenerator(root_dir=fresh_root)
    task_manager = TaskManager(root_dir=fresh_root)
    metrics_sampler = SystemMonitor()

    port = int(_settings.get("ui_port", 8080))
    ui.run(
        title="Pyruns Experiment Lab",
        port=port, show=True, reload=False, favicon="🧪",
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
