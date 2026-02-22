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
from pyruns.utils import get_logger
from nicegui import app

logger = get_logger(__name__)

# Serve static files (JS, CSS)
static_dir = os.path.join(os.path.dirname(__file__), "static")
app.add_static_files("/static", static_dir)

# Global singletons (initialised lazily in main())
task_generator: TaskGenerator = None  # type: ignore
task_manager: TaskManager = None  # type: ignore
metrics_sampler: SystemMonitor = None  # type: ignore
_settings: dict = {}  # workspace settings from _pyruns_.yaml


@ui.page("/")
def main_page():
    """Per-session page — creates mutable state and routes to the active tab."""
    logger.debug("Creating new session page")
    state = asdict(AppState(_settings=_settings))

    # Each renderer is called at most once (on first visit).
    # Subsequent tab switches just toggle CSS visibility — instant.
    page_renderers = {
        "generator": lambda: render_generator_page(
            state,
            task_generator,
            task_manager,
        ),
        "manager": lambda: render_manager_page(state, task_manager),
        "monitor": lambda: render_monitor_page(state, task_manager),
    }

    render_main_layout(state, task_manager, metrics_sampler, page_renderers)


def main(*, reload: bool = False):
    """Bootstrap the app: read env vars, create singletons, start server.

    Parameters
    ----------
    reload : bool
        Enable NiceGUI hot-reload (dev mode).  Only works when the module
        is executed directly (``python -m pyruns.ui.app``), NOT via the
        ``pyr`` CLI entry point.
    """
    global task_generator, task_manager, metrics_sampler, _settings

    # Re-read ROOT_DIR from env at runtime (cli.py may have set it after
    # _config.py was first imported).
    import pyruns._config as _cfg

    fresh_root = os.getenv(_cfg.ENV_ROOT, _cfg.ROOT_DIR)
    _cfg.ROOT_DIR = fresh_root
    abs_tasks_dir = os.path.join(fresh_root, _cfg.TASKS_DIR)
    os.makedirs(abs_tasks_dir, exist_ok=True)

    # Load workspace settings
    from pyruns.utils.settings import load_settings, ensure_settings_file

    ensure_settings_file(fresh_root)
    _settings = load_settings(fresh_root)

    logger.debug("--- DIRECTORY DEBUG ---")
    logger.debug(f"fresh_root: {fresh_root}")
    logger.debug(f"_cfg.ROOT_DIR: {_cfg.ROOT_DIR}")
    logger.debug(f"_cfg.TASKS_DIR: {_cfg.TASKS_DIR}")

    task_generator = TaskGenerator(root_dir=abs_tasks_dir)
    logger.debug(f"TaskGenerator instantiated with root_dir={task_generator.root_dir}")
    task_manager = TaskManager(root_dir=abs_tasks_dir)
    metrics_sampler = SystemMonitor()
    logger.info("Pyruns initialised  root=%s  port=%s", fresh_root, _settings.get("ui_port", 8080))

    port = int(_settings.get("ui_port", 8080))
    # In dev mode, tell uvicorn to watch the pyruns source tree for changes.
    pkg_dir = os.path.dirname(os.path.dirname(__file__))  # …/pyruns/
    ui.run(
        title="Pyruns Experiment Lab",
        port=port,
        show=True,
        reload=reload,
        uvicorn_reload_dirs=pkg_dir if reload else ".",
        favicon="🧪",
    )


if __name__ in {"__main__", "__mp_main__"}:
    main(reload=True)
