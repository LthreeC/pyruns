"""
Left sidebar navigation — Generator / Manager / Monitor tabs.
"""
from nicegui import ui
from typing import Dict, Any, Callable

_TABS = [
    ("Generator", "add_circle", "generator"),
    ("Manager", "dns", "manager"),
    ("Monitor", "monitor_heart", "monitor"),
]


def render_sidebar(state: Dict[str, Any], refresh_content: Callable) -> None:
    """Render the left navigation drawer."""
    with ui.left_drawer(value=True).classes(
        "bg-white border-r border-slate-100 "
        "shadow-[4px_0_24px_rgba(0,0,0,0.02)] print:hidden"
    ).style("width: 110px; min-width: 110px;"):
        ui.label("MENU").classes(
            "text-[9px] font-bold text-slate-400 px-2 mt-5 mb-2 tracking-wider"
        )

        @ui.refreshable
        def menu() -> None:
            with ui.column().classes("w-full gap-0"):
                for name, icon, tab in _TABS:
                    _nav_item(name, icon, tab, state, refresh_content, menu)

        menu()

        with ui.column().classes("absolute bottom-3 w-full px-2"):
            ui.label("v0.1").classes("text-[9px] text-slate-300 font-mono")


def _nav_item(
    name: str, icon: str, tab: str,
    state: dict, refresh_content: Callable, menu_refreshable,
) -> None:
    """Single navigation button."""
    active = state["active_tab"] == tab
    base = (
        "w-full px-2 py-1.5 rounded-r-full transition-all duration-200 "
        "flex items-center gap-1 text-[20px] font-medium mr-0.5"
    )
    if active:
        cls = (
            f"{base} bg-indigo-50 text-indigo-700 "
            "border-l-3 border-indigo-600"
        )
        icon_color = "text-indigo-600"
    else:
        cls = (
            f"{base} text-slate-500 hover:bg-slate-50 hover:text-slate-800 "
            "border-l-3 border-transparent"
        )
        icon_color = "text-slate-400"

    with ui.button(
        on_click=lambda: (
            state.update({"active_tab": tab}),
            refresh_content(),
            menu_refreshable.refresh(),
        ),
    ).props("flat no-caps").classes(cls):
        ui.icon(icon).classes(f"{icon_color} text-sm")
        ui.label(name).classes("truncate")
