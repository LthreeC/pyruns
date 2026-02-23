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

    ui.context.client.content.classes("p-0 gap-0")

    # ── Flex row: sidebar (15%) + content (85%) ──
    with ui.row().classes("w-full flex-nowrap gap-0").style("height: calc(100vh - 52px); overflow: hidden;"):
        # Sidebar column
        render_sidebar(state, lambda tab: switch_tab(tab))

        # Content column — fills the remaining space (relative anchor for absolute children)
        with ui.column().classes("flex-grow min-w-0 gap-0 h-full relative").style("height: 100%;"):
            containers: Dict[str, ui.element] = {}
            rendered: set = set()

            for tab in _TAB_NAMES:
                c = ui.column().classes(
                    "w-full h-full gap-0 flex-nowrap overflow-hidden absolute inset-0 "
                    "transition-opacity duration-200 ease-in-out"
                ).style("height: 100%; min-height: 0;")
                c.classes("opacity-0 pointer-events-none")
                containers[tab] = c

    def switch_tab(tab: str) -> None:
        """Show *tab* and hide the rest; lazy-render on first visit."""
        state["active_tab"] = tab

        # Notify tab change observers (if any)
        for cb in state.get("on_tab_change", []):
            try:
                res = cb(tab)
                if hasattr(res, "__await__"):
                    from nicegui.background_tasks import create
                    create(res)
            except Exception:
                pass
                
        # Broadcast globally via our new simple event bus
        from pyruns.utils.events import event_sys
        event_sys.emit("on_tab_change", tab)

        # Toggle opacity instead of v-if visibility to allow CSS transitions
        for name, c in containers.items():
            if name == tab:
                c.classes(remove="opacity-0 pointer-events-none", add="opacity-100 z-10")
            else:
                c.classes(remove="opacity-100 z-10", add="opacity-0 pointer-events-none")

        # Render content once, on first visit
        if tab not in rendered:
            rendered.add(tab)
            with containers[tab]:
                page_renderers[tab]()

    # Render & show the initial tab
    initial = state["active_tab"]
    
    # We must mark ALL tabs as rendered so they pre-build into the HTML,
    # otherwise the CSS opacity fade won't work on first click because 
    # the DOM node doesn't exist yet.
    for tab in _TAB_NAMES:
        rendered.add(tab)
        with containers[tab]:
            page_renderers[tab]()
            
    containers[initial].classes(
        remove="opacity-0 pointer-events-none", add="opacity-100 z-10"
    )
