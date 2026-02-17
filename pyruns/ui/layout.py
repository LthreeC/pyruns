"""
Main layout — header + sidebar + lazy-rendered content panels.

Instead of destroying / recreating pages on every tab switch
(``@ui.refreshable``), we create one container per page and toggle
CSS visibility.  Pages are rendered **lazily** on first visit so
the initial load stays fast.

This eliminates the multi-second lag when switching tabs — the DOM
tree for already-visited pages is preserved across switches.
"""

from nicegui import ui
from typing import Dict, Any, Callable


from pyruns.ui.components.header import render_header
from pyruns.ui.components.sidebar import render_sidebar

_TAB_NAMES = ("generator", "manager", "monitor")


def render_main_layout(
    state: Dict[str, Any],
    task_manager,
    metrics_sampler,
    page_renderers: Dict[str, Callable],
) -> None:
    """Assemble the full page: header, sidebar, and lazy content panels."""
    render_header(state, metrics_sampler)

    # ── Flex row: sidebar (15%) + content (85%) ──
    with ui.row().classes("w-full flex-nowrap gap-0").style("height: calc(100vh - 52px); overflow: hidden;"):
        # Sidebar column
        render_sidebar(state, lambda tab: switch_tab(tab))

        # Content column — fills the remaining space
        with ui.column().classes("flex-grow min-w-0 gap-0"):
            containers: Dict[str, ui.element] = {}
            rendered: set = set()

            for tab in _TAB_NAMES:
                c = ui.column().classes(f"w-full gap-0 flex-grow overflow-y-auto")
                c.set_visibility(False)
                containers[tab] = c

    def switch_tab(tab: str) -> None:
        """Show *tab* and hide the rest; lazy-render on first visit."""
        state["active_tab"] = tab

        # Toggle visibility (instant CSS show/hide, no DOM rebuild)
        for name, c in containers.items():
            c.set_visibility(name == tab)

        # Render content once, on first visit
        if tab not in rendered:
            rendered.add(tab)
            with containers[tab]:
                page_renderers[tab]()

    # Render & show the initial tab
    initial = state["active_tab"]
    rendered.add(initial)
    containers[initial].set_visibility(True)
    with containers[initial]:
        page_renderers[initial]()
