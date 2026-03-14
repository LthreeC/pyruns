"""Main layout with header, sidebar, and lazy-rendered page panels."""

from typing import Any, Callable, Dict

from nicegui import ui

from pyruns.ui.components.header import render_header
from pyruns.ui.components.sidebar import render_sidebar

_TAB_NAMES = ("generator", "manager", "monitor")


def render_main_layout(
    state: Dict[str, Any],
    task_manager,
    metrics_sampler,
    page_renderers: Dict[str, Callable],
) -> None:
    """Assemble the full page shell and lazily mount each tab once."""
    render_header(state, metrics_sampler)
    ui.context.client.content.classes("p-0 gap-0")

    with ui.row().classes("w-full gap-0 flex-nowrap app-shell").style("height: calc(100vh - 52px); overflow: hidden;"):
        render_sidebar(state, lambda tab: switch_tab(tab))

        with ui.column().classes("flex-grow min-w-0 gap-0 h-full relative app-content").style("height: 100%;"):
            containers: Dict[str, ui.element] = {}
            rendered: set[str] = set()

            for tab in _TAB_NAMES:
                container = ui.column().classes(
                    "w-full h-full gap-0 flex-nowrap overflow-hidden absolute inset-0 "
                    "transition-opacity duration-200 ease-in-out"
                ).style("height: 100%; min-height: 0;")
                container.classes("opacity-0 pointer-events-none")
                containers[tab] = container

    def switch_tab(tab: str) -> None:
        """Show the requested tab and keep others mounted but hidden."""
        state["active_tab"] = tab

        for callback in state.get("on_tab_change", []):
            try:
                result = callback(tab)
                if hasattr(result, "__await__"):
                    from nicegui.background_tasks import create

                    create(result)
            except Exception:
                pass

        from pyruns.utils.events import event_sys

        event_sys.emit("on_tab_change", tab)

        for name, container in containers.items():
            if name == tab:
                container.classes(remove="opacity-0 pointer-events-none", add="opacity-100 z-10")
            else:
                container.classes(remove="opacity-100 z-10", add="opacity-0 pointer-events-none")

        if tab not in rendered:
            rendered.add(tab)
            with containers[tab]:
                page_renderers[tab]()

    switch_tab(state["active_tab"])
