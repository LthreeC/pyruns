from nicegui import ui
from typing import Dict, Any
from pyruns._config import HEADER_GRADIENT

def render_header(state: Dict[str, Any], metrics_sampler) -> None:
    with ui.header().classes(
        f"{HEADER_GRADIENT} text-white px-6 py-2 shadow-md border-b border-white/10 items-center justify-between"
    ):
        with ui.row().classes("items-center gap-3"):
            ui.icon("rocket_launch", size="28px", color="white")
            ui.label("PYRUNS LAB").classes("text-xl font-bold tracking-widest font-mono text-white/90")

        with ui.row().classes("items-center gap-4"):
            with ui.row().classes("items-center gap-1 bg-white/10 px-2 py-0.5 rounded border border-white/10"):
                ui.icon("timer", size="xs", color="white/70")
                ui.label("REFRESH:").classes("text-[10px] font-bold text-white/70")
                refresh_input = ui.number(
                    value=state["refresh_interval"],
                    min=1.0,
                    max=60.0,
                    step=0.5,
                    format="%.1f",
                ).props("dense outlined input-style='width:70px'").classes("text-white/90")
                refresh_input.on_value_change(
                    lambda e: state.update({"refresh_interval": float(e.value) if e.value else state["refresh_interval"]})
                )
                 

            expand_btn = ui.button(
                icon="unfold_less" if state.get("metrics_expanded") else "unfold_more",
            ).props("flat round dense color=white").classes("text-white/80")
            def toggle_expand():
                state["metrics_expanded"] = not state.get("metrics_expanded")
                expand_btn.props(
                    f"icon={'unfold_less' if state.get('metrics_expanded') else 'unfold_more'}"
                )
            expand_btn.on_click(toggle_expand)

            @ui.refreshable
            def metrics_row() -> None:
                m = metrics_sampler.sample()
                with ui.row().classes("items-center gap-3 bg-white/5 px-3 py-1 rounded-full border border-white/10 backdrop-blur-sm"):
                    def stat(label, val, icon=None):
                        with ui.row().classes("items-center gap-1 bg-white/5 px-2 py-0.5 rounded-full border border-white/10"):
                            if icon:
                                ui.icon(icon, size="xs", color="gray-300")
                            ui.label(label).classes("text-[10px] font-bold text-gray-400 uppercase tracking-wider")
                            ui.label(val).classes("text-xs font-mono text-white")

                    stat("CPU", f"{m['cpu_percent']:.0f}%", "memory")
                    stat("RAM", f"{m['mem_percent']:.0f}%", "memory")
                    
                    gpus = m["gpus"] or []
                    show_inline = gpus if len(gpus) <= 2 else gpus[:2]
                    for gpu in show_inline:
                        mem_pct = (gpu["mem_used"] / gpu["mem_total"] * 100.0) if gpu["mem_total"] else 0.0
                        stat(
                            f"GPU{gpu['index']}",
                            f"{gpu['util']:.0f}% | {gpu['mem_used']:.0f}/{gpu['mem_total']:.0f}MB",
                            "developer_board",
                        )
                    show_expand = len(gpus) > 2
                    if hasattr(expand_btn, "set_visibility"):
                        expand_btn.set_visibility(show_expand)
                    else:
                        expand_btn.props("style=display:none" if not show_expand else "style=display:inline-flex")

                if state.get("metrics_expanded") and len(m["gpus"] or []) > 2:
                    with ui.row().classes("mt-2 gap-2 flex-wrap"):
                        for gpu in m["gpus"][2:]:
                            mem_pct = (gpu["mem_used"] / gpu["mem_total"] * 100.0) if gpu["mem_total"] else 0.0
                            with ui.row().classes(
                                "items-center gap-2 bg-white/5 px-3 py-1 rounded-full border border-white/10"
                            ):
                                ui.icon("developer_board", size="xs", color="gray-300")
                                ui.label(f"GPU{gpu['index']}").classes("text-[10px] font-bold text-gray-400 uppercase")
                                ui.label(
                                    f"{gpu['util']:.0f}% | {gpu['mem_used']:.0f}/{gpu['mem_total']:.0f}MB ({mem_pct:.0f}%)"
                                ).classes("text-xs font-mono text-white")
                
                ui.timer(state["refresh_interval"], metrics_row.refresh, once=True)

            metrics_row()
