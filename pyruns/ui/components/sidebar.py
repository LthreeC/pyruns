"""
Left sidebar navigation — Generator / Manager / Monitor tabs.
"""
from nicegui import ui
from typing import Dict, Any, Callable

from pyruns.ui.theme import SIDEBAR_WIDTH

_TABS = [
    ("Generator", "add_circle", "generator"),
    ("Manager", "dns", "manager"),
    ("Monitor", "monitor_heart", "monitor"),
]


def render_sidebar(state: Dict[str, Any], switch_tab: Callable) -> None:
    """Render the left navigation column.

    Parameters
    ----------
    switch_tab : Callable[[str], None]
        Callback that toggles container visibility (no DOM rebuild).
    """
    with ui.column().classes(
        "flex-none bg-white border-r border-slate-100 gap-0 "
        "shadow-[4px_0_24px_rgba(0,0,0,0.02)] print:hidden"
    ).style(f"width: {SIDEBAR_WIDTH}; min-height: 100%;"):
        ui.label("MENU").classes(
            "text-[9px] font-bold text-slate-400 px-2 mt-5 mb-2 tracking-wider"
        )

        @ui.refreshable
        def menu() -> None:
            with ui.column().classes("w-full gap-0 px-1.5"):
                for name, icon, tab in _TABS:
                    _nav_item(name, icon, tab, state, switch_tab, menu)

        menu()


def _nav_item(
    name: str, icon: str, tab: str,
    state: dict, switch_tab: Callable, menu_refreshable,
) -> None:
    """Single navigation button."""
    active = state["active_tab"] == tab
    base = (
        "w-full px-2.5 py-1.5 transition-all duration-200 "
        "flex items-center gap-2 text-[18px] font-medium"
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
            switch_tab(tab),
            menu_refreshable.refresh(),
        ),
    ).props("flat no-caps").classes(cls):
        ui.icon(icon).classes(f"{icon_color} text-sm")
        ui.label(name).classes("truncate")

