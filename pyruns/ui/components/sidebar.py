"""
Left sidebar navigation — Generator / Manager / Monitor tabs.
"""
from nicegui import ui
from typing import Dict, Any, Callable

from pyruns.ui.theme import (
    SIDEBAR_WIDTH, SIDEBAR_COL_CLASSES, SIDEBAR_MENU_TITLE_CLASSES,
    SIDEBAR_LIST_CLASSES, SIDEBAR_BTN_PROPS, SIDEBAR_ICON_ACTIVE,
    SIDEBAR_ICON_INACTIVE, SIDEBAR_LABEL_CLASSES,
)

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
    with ui.column().classes(SIDEBAR_COL_CLASSES).style(f"width: {SIDEBAR_WIDTH}; min-height: 100%;"):
        ui.label("MENU").classes(SIDEBAR_MENU_TITLE_CLASSES)

        @ui.refreshable
        def menu() -> None:
            with ui.column().classes(SIDEBAR_LIST_CLASSES):
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
        icon_color = SIDEBAR_ICON_ACTIVE
    else:
        cls = (
            f"{base} text-slate-500 hover:bg-slate-50 hover:text-slate-800 "
            "border-l-3 border-transparent"
        )
        icon_color = SIDEBAR_ICON_INACTIVE

    with ui.button(
        on_click=lambda: (
            switch_tab(tab),
            menu_refreshable.refresh(),
        ),
    ).props(SIDEBAR_BTN_PROPS).classes(cls):
        ui.icon(icon).classes(icon_color)
        ui.label(name).classes(SIDEBAR_LABEL_CLASSES)

