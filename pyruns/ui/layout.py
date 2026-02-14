"""
Main layout — header + sidebar + refreshable content area.
"""
from nicegui import ui
from typing import Dict, Any

from pyruns._config import BG_COLOR
from pyruns.ui.components.header import render_header
from pyruns.ui.components.sidebar import render_sidebar


def render_main_layout(
    state: Dict[str, Any],
    task_manager,
    metrics_sampler,
    page_content_func,
) -> None:
    """Assemble the full page: header, sidebar, and refreshable content."""
    render_header(state, metrics_sampler)

    @ui.refreshable
    def content() -> None:
        if state.get("active_tab") == "monitor":
            # Monitor manages its own layout and height
            page_content_func()
        else:
            with ui.column().classes(f"w-full px-5 py-4 {BG_COLOR} min-h-screen"):
                page_content_func()

    render_sidebar(state, content.refresh)
    content()
