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

# Global Singletons
task_generator = TaskGenerator()
task_manager = TaskManager()
metrics_sampler = SystemMonitor()

@ui.page("/")
def main_page():
    # Per-session state
    state = asdict(AppState()) # Convert to dict for mutability
    
    def page_router():
        if state["active_tab"] == "generator":
            render_generator_page(state, task_generator, task_manager)
        elif state["active_tab"] == "manager":
            render_manager_page(state, task_manager)
        elif state["active_tab"] == "monitor":
            render_monitor_page(state, task_manager)
            
    render_main_layout(state, task_manager, metrics_sampler, page_router)

def main():
    ui.run(title="Pyruns Experiment Lab", port=8080, show=True, reload=False, favicon="🧪")

if __name__ in {"__main__", "__mp_main__"}:
    main()
