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

from pyruns._config import BG_COLOR
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

    # ── Create one container per page ──
    containers: Dict[str, ui.element] = {}
    rendered: set = set()

    for tab in _TAB_NAMES:
        if tab == "monitor":
            # Monitor manages its own height / overflow — no padding wrapper
            c = ui.column().classes("w-full gap-0")
        else:
            c = ui.column().classes(
                f"w-full px-5 py-4 {BG_COLOR} min-h-screen"
            )
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

    render_sidebar(state, switch_tab)
