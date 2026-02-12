from nicegui import ui
from typing import Dict, Any

from pyruns._config import BG_COLOR
from pyruns.ui.components.header import render_header
from pyruns.ui.components.sidebar import render_sidebar

def render_main_layout(state: Dict[str, Any], task_manager, metrics_sampler, page_content_func) -> None:
    # Header
    render_header(state, metrics_sampler)
    
    # Refreshable Content Wrapper
    @ui.refreshable
    def content() -> None:
        with ui.column().classes(f"w-full p-8 {BG_COLOR} min-h-screen"):
            page_content_func()

    # Sidebar (Passes refresh handle)
    render_sidebar(state, content.refresh)
    
    # Initial Content
    content()
