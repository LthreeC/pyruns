from nicegui import ui
from typing import Dict, Any, Callable

def render_sidebar(state: Dict[str, Any], refresh_content: Callable) -> None:
    """Render the left sidebar navigation drawer."""
    
    with ui.left_drawer(value=True).classes("bg-white border-r border-slate-100 w-64 shadow-[4px_0_24px_rgba(0,0,0,0.02)] print:hidden"):
        ui.label("MAIN MENU").classes("text-[10px] font-bold text-slate-400 px-6 mt-8 mb-4 tracking-wider")

        @ui.refreshable
        def render_menu() -> None:
            def nav(name: str, icon: str, tab: str) -> None:
                active = state["active_tab"] == tab
                base_classes = "w-full px-6 py-3 rounded-r-full transition-all duration-200 flex items-center gap-3 text-sm font-medium mr-4"
                
                if active:
                    style = f"{base_classes} bg-indigo-50 text-indigo-700 border-l-4 border-indigo-600"
                    icon_color = "text-indigo-600"
                else:
                    style = f"{base_classes} text-slate-500 hover:bg-slate-50 hover:text-slate-800 border-l-4 border-transparent"
                    icon_color = "text-slate-400 group-hover:text-slate-600"

                with ui.button(on_click=lambda: (
                    state.update({"active_tab": tab}),
                    refresh_content(),
                    render_menu.refresh(),
                )).props("flat no-caps").classes(style):
                    ui.icon(icon).classes(f"{icon_color} text-lg")
                    ui.label(name)

            with ui.column().classes("w-full gap-1"):
                nav("Task Generator", "add_circle_outline", "generator")
                nav("Task Manager", "dns", "manager")

        render_menu()
        
        # Bottom branding
        with ui.column().classes("absolute bottom-4 w-full px-6"):
             ui.label("v0.3.0").classes("text-[10px] text-slate-300 font-mono")
