from nicegui import ui
from typing import Dict, Any
from pyruns.utils.config_utils import parse_value
from pyruns.ui.theme import INPUT_PROPS

def recursive_param_editor(container, data: Dict[str, Any], state: Dict[str, Any], task_manager, depth: int = 0) -> None:
    with container:
        simple_keys = {k: v for k, v in data.items() if not isinstance(v, dict) and k != "_meta_desc"}
        dict_keys = {k: v for k, v in data.items() if isinstance(v, dict)}

        if simple_keys:
            bg_class = "bg-white" if depth == 0 else "bg-slate-50/50"
            border_class = "border-slate-100" if depth == 0 else "border-slate-200/50"
            
            with ui.card().classes(f"w-full {bg_class} p-4 border {border_class} rounded-xl shadow-sm mb-3"):
                for key, value in simple_keys.items():
                    if key.startswith("_"): continue
                        
                    with ui.grid(columns=12).classes("w-full items-center gap-4 mb-2"):
                        with ui.row().classes("col-span-4 items-center"):
                            ui.label(key).classes("text-sm font-medium text-slate-600")

                        def on_change_wrapper(e, d=data, k=key):
                            new_val = parse_value(e.value)
                            d.update({k: new_val})

                        item_props = INPUT_PROPS
                        text_class = "text-slate-800 font-mono text-sm"
                        if isinstance(value, (int, float)):
                            text_class += " text-blue-700"
                        if isinstance(value, list):
                            text_class += " text-purple-700"
                        if isinstance(value, bool):
                            ui.switch(value=value, on_change=on_change_wrapper).props("dense color=indigo").classes("col-span-8")
                        else:
                            ui.input(value=str(value), on_change=on_change_wrapper).props(item_props).classes(f"col-span-8 w-full {text_class} bg-white")

        for key, value in dict_keys.items():
            with ui.expansion(f"{key.upper()}", icon="folder", value=True).classes(
                "w-full mb-3 border border-slate-200 rounded-xl overflow-hidden bg-white shadow-sm"
            ).props("header-class='bg-slate-50 text-slate-700 font-bold tracking-wide'"):
                with ui.column().classes("w-full p-3 border-t border-slate-100"):
                    recursive_param_editor(ui.column().classes("w-full"), value, state, task_manager, depth + 1)
