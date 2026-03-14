"""Left sidebar navigation for Generator / Manager / Monitor tabs."""

from typing import Any, Callable, Dict

from nicegui import ui

from pyruns.ui.theme import (
    SIDEBAR_COL_CLASSES,
    SIDEBAR_ICON_ACTIVE,
    SIDEBAR_ICON_INACTIVE,
    SIDEBAR_LIST_CLASSES,
    SIDEBAR_BTN_PROPS,
    SIDEBAR_WIDTH,
)

_TABS = [
    ("Generator", "add_circle", "generator"),
    ("Manager", "dns", "manager"),
    ("Monitor", "monitor_heart", "monitor"),
]


def render_sidebar(state: Dict[str, Any], switch_tab: Callable) -> None:
    """Render the left navigation column."""
    with ui.column().classes(SIDEBAR_COL_CLASSES).style(
        f"width: {SIDEBAR_WIDTH}; min-width: 118px; max-width: 146px; min-height: 100%;"
    ):
        ui.label("MENU").classes("text-[9px] font-bold text-slate-400 px-2 mt-5 mb-2 tracking-wider")

        @ui.refreshable
        def menu() -> None:
            with ui.column().classes(SIDEBAR_LIST_CLASSES):
                for name, icon, tab in _TABS:
                    _nav_item(name, icon, tab, state, switch_tab, menu)

        menu()


def _nav_item(
    name: str,
    icon: str,
    tab: str,
    state: Dict[str, Any],
    switch_tab: Callable,
    menu_refreshable,
) -> None:
    """Render one navigation button."""
    active = state["active_tab"] == tab
    base_classes = (
        "w-full px-2.5 py-2 transition-all duration-200 "
        "flex items-center gap-2 text-sm font-semibold justify-start sidebar-nav-btn"
    )

    if active:
        button_classes = (
            f"{base_classes} bg-indigo-50 text-indigo-700 "
            "border-l-3 border-indigo-600"
        )
        icon_classes = SIDEBAR_ICON_ACTIVE
    else:
        button_classes = (
            f"{base_classes} text-slate-500 hover:bg-slate-50 hover:text-slate-800 "
            "border-l-3 border-transparent"
        )
        icon_classes = SIDEBAR_ICON_INACTIVE

    with ui.button(
        on_click=lambda: (switch_tab(tab), menu_refreshable.refresh()),
    ).props(SIDEBAR_BTN_PROPS).classes(button_classes):
        with ui.row().classes("items-center gap-2 no-wrap w-full"):
            ui.icon(icon).classes(icon_classes)
            ui.label(name).classes("truncate text-[12px]")
