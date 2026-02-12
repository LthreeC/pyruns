from nicegui import ui
from typing import Dict, Any, List
from pyruns._config import HEADER_GRADIENT


def _gpu_bar(gpu: Dict[str, Any], compact: bool = False) -> None:
    """Render a single GPU status chip with utilization and memory bars."""
    util = gpu["util"]
    mem_used = gpu["mem_used"]
    mem_total = gpu["mem_total"]
    mem_pct = (mem_used / mem_total * 100.0) if mem_total else 0.0

    # Color based on utilization
    if util >= 80:
        util_color = "#ef4444"   # red
    elif util >= 50:
        util_color = "#f59e0b"   # amber
    else:
        util_color = "#22c55e"   # green

    if mem_pct >= 85:
        mem_color = "#ef4444"
    elif mem_pct >= 60:
        mem_color = "#f59e0b"
    else:
        mem_color = "#3b82f6"    # blue

    if compact:
        # Ultra-compact: just index + util% + mem%
        with ui.row().classes(
            "items-center gap-1.5 bg-white/5 px-2 py-0.5 rounded border border-white/10"
        ):
            ui.label(f"G{gpu['index']}").classes(
                "text-[9px] font-bold text-gray-400 font-mono w-4"
            )
            # Utilization mini bar
            with ui.element("div").classes("rounded-full overflow-hidden").style(
                "width:32px; height:5px; background:rgba(255,255,255,0.1);"
            ):
                ui.element("div").style(
                    f"width:{util:.0f}%; height:100%; background:{util_color}; "
                    f"border-radius:9999px;"
                )
            ui.label(f"{util:.0f}%").classes("text-[9px] font-mono text-white/80 w-6")
            # Memory mini bar
            with ui.element("div").classes("rounded-full overflow-hidden").style(
                "width:32px; height:5px; background:rgba(255,255,255,0.1);"
            ):
                ui.element("div").style(
                    f"width:{mem_pct:.0f}%; height:100%; background:{mem_color}; "
                    f"border-radius:9999px;"
                )
            ui.label(f"{mem_pct:.0f}%").classes("text-[9px] font-mono text-white/80 w-6")
    else:
        # Normal: full details
        with ui.row().classes(
            "items-center gap-2 bg-white/5 px-2.5 py-1 rounded-lg border border-white/10"
        ):
            ui.icon("developer_board", size="14px").classes("text-gray-400")
            ui.label(f"GPU{gpu['index']}").classes(
                "text-[10px] font-bold text-gray-300 uppercase w-8"
            )
            # Utilization bar
            with ui.column().classes("gap-0"):
                ui.label("Util").classes("text-[8px] text-gray-500 leading-none")
                with ui.row().classes("items-center gap-1"):
                    with ui.element("div").classes("rounded-full overflow-hidden").style(
                        "width:40px; height:6px; background:rgba(255,255,255,0.1);"
                    ):
                        ui.element("div").style(
                            f"width:{util:.0f}%; height:100%; background:{util_color}; "
                            f"border-radius:9999px;"
                        )
                    ui.label(f"{util:.0f}%").classes("text-[10px] font-mono text-white w-7")
            # Memory bar
            with ui.column().classes("gap-0"):
                ui.label("Mem").classes("text-[8px] text-gray-500 leading-none")
                with ui.row().classes("items-center gap-1"):
                    with ui.element("div").classes("rounded-full overflow-hidden").style(
                        "width:40px; height:6px; background:rgba(255,255,255,0.1);"
                    ):
                        ui.element("div").style(
                            f"width:{mem_pct:.0f}%; height:100%; background:{mem_color}; "
                            f"border-radius:9999px;"
                        )
                    ui.label(
                        f"{mem_used:.0f}/{mem_total:.0f}M"
                    ).classes("text-[9px] font-mono text-white/80")


def render_header(state: Dict[str, Any], metrics_sampler) -> None:
    with ui.header().classes(
        f"{HEADER_GRADIENT} text-white px-6 py-2 shadow-md border-b border-white/10 "
        "items-center justify-between"
    ):
        with ui.row().classes("items-center gap-3"):
            ui.icon("rocket_launch", size="28px", color="white")
            ui.label("PYRUNS LAB").classes(
                "text-xl font-bold tracking-widest font-mono text-white/90"
            )

        with ui.row().classes("items-center gap-4"):
            # Refresh interval
            with ui.row().classes(
                "items-center gap-1 bg-white/10 px-2 py-0.5 rounded border border-white/10"
            ):
                ui.icon("timer", size="xs", color="white/70")
                ui.label("REFRESH:").classes("text-[10px] font-bold text-white/70")
                refresh_input = ui.number(
                    value=state["refresh_interval"],
                    min=1.0, max=60.0, step=0.5, format="%.1f",
                ).props("dense outlined input-style='width:70px'").classes("text-white/90")
                refresh_input.on_value_change(
                    lambda e: state.update({
                        "refresh_interval": float(e.value)
                        if e.value else state["refresh_interval"]
                    })
                )

            @ui.refreshable
            def metrics_row() -> None:
                m = metrics_sampler.sample()

                with ui.row().classes(
                    "items-center gap-3 bg-white/5 px-3 py-1 "
                    "rounded-full border border-white/10 backdrop-blur-sm"
                ):
                    # ── CPU / RAM stats ──
                    def stat(label, val, icon_name=None):
                        with ui.row().classes(
                            "items-center gap-1 bg-white/5 px-2 py-0.5 "
                            "rounded-full border border-white/10"
                        ):
                            if icon_name:
                                ui.icon(icon_name, size="xs", color="gray-300")
                            ui.label(label).classes(
                                "text-[10px] font-bold text-gray-400 uppercase tracking-wider"
                            )
                            ui.label(val).classes("text-xs font-mono text-white")

                    stat("CPU", f"{m['cpu_percent']:.0f}%", "memory")
                    stat("RAM", f"{m['mem_percent']:.0f}%", "memory")
                    
                    # ── GPU section ──
                    gpus: List[Dict[str, Any]] = m.get("gpus") or []
                    n_gpus = len(gpus)

                    if n_gpus == 0:
                        # No GPU detected
                        pass
                    elif n_gpus <= 4:
                        # ≤4 GPUs: show all inline with bars
                        for gpu in gpus:
                            _gpu_bar(gpu, compact=(n_gpus > 2))
                    else:
                        # >4 GPUs (multi-GPU server): compact summary + expand
                        # Show brief aggregate
                        avg_util = sum(g["util"] for g in gpus) / n_gpus
                        total_mem = sum(g["mem_total"] for g in gpus)
                        used_mem = sum(g["mem_used"] for g in gpus)
                        mem_pct = (used_mem / total_mem * 100) if total_mem else 0

                        with ui.row().classes(
                            "items-center gap-2 bg-white/5 px-2.5 py-1 "
                            "rounded-lg border border-white/10"
                            ):
                            ui.icon("developer_board", size="14px").classes("text-gray-400")
                            ui.label(f"{n_gpus} GPUs").classes(
                                "text-[10px] font-bold text-gray-300 uppercase"
                            )
                            ui.label(
                                f"Avg {avg_util:.0f}% | {used_mem:.0f}/{total_mem:.0f}M ({mem_pct:.0f}%)"
                            ).classes("text-[10px] font-mono text-white/80")

                # ── Expanded GPU panel (for >2 GPUs) ──
                if state.get("metrics_expanded") and n_gpus > 2:
                    with ui.element("div").classes(
                        "w-full mt-2 px-2 py-2 bg-white/5 rounded-xl "
                        "border border-white/10 backdrop-blur-sm"
                    ):
                        # Adaptive grid: 4 columns for ≤8, 8 columns for many
                        grid_cols = 4 if n_gpus <= 8 else min(8, n_gpus)
                        with ui.grid(columns=grid_cols).classes("w-full gap-1.5"):
                            for gpu in gpus:
                                _gpu_bar(gpu, compact=True)
                
                ui.timer(state["refresh_interval"], metrics_row.refresh, once=True)

            # Expand/collapse button (visibility controlled inside metrics_row)
            expand_btn = ui.button(
                icon="unfold_less" if state.get("metrics_expanded") else "unfold_more",
            ).props("flat round dense color=white").classes("text-white/80")

            def toggle_expand():
                state["metrics_expanded"] = not state.get("metrics_expanded", False)
                expand_btn.props(
                    f"icon={'unfold_less' if state['metrics_expanded'] else 'unfold_more'}"
                )
                metrics_row.refresh()

            expand_btn.on_click(toggle_expand)

            metrics_row()
