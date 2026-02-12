from nicegui import ui
from typing import Dict, Any, Callable

def render_sidebar(state: Dict[str, Any], refresh_content: Callable) -> None:
    """Render the left sidebar navigation drawer."""
    
    with ui.left_drawer(value=True).classes(
        "bg-white border-r border-slate-100 shadow-[4px_0_24px_rgba(0,0,0,0.02)] print:hidden"
    ).style("width: 110px; min-width: 110px;"):
        ui.label("MENU").classes("text-[9px] font-bold text-slate-400 px-2 mt-5 mb-2 tracking-wider")

        @ui.refreshable
        def render_menu() -> None:
            def nav(name: str, icon: str, tab: str) -> None:
                active = state["active_tab"] == tab
                base_classes = (
                    "w-full px-2 py-1.5 rounded-r-full transition-all duration-200 "
                    "flex items-center gap-1 text-[20px] font-medium mr-0.5"
                )

                if active:
                    style = f"{base_classes} bg-indigo-50 text-indigo-700 border-l-3 border-indigo-600"
                    icon_color = "text-indigo-600"
                else:
                    style = f"{base_classes} text-slate-500 hover:bg-slate-50 hover:text-slate-800 border-l-3 border-transparent"
                    icon_color = "text-slate-400"

                with ui.button(on_click=lambda: (
                    state.update({"active_tab": tab}),
                    refresh_content(),
                    render_menu.refresh(),
                )).props("flat no-caps").classes(style):
                    ui.icon(icon).classes(f"{icon_color} text-sm")
                    ui.label(name).classes("truncate")

            with ui.column().classes("w-full gap-0"):
                nav("Generator", "add_circle", "generator")
                nav("Manager", "dns", "manager")
                nav("Monitor", "monitor_heart", "monitor")

        render_menu()

        # Bottom branding
        with ui.column().classes("absolute bottom-3 w-full px-2"):
            ui.label("v0.1").classes("text-[9px] text-slate-300 font-mono")
