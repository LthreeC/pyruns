from nicegui import ui
from typing import Dict, Any, List
from pyruns._config import HEADER_GRADIENT

# Fixed refresh interval (seconds)
_METRICS_REFRESH_INTERVAL = 3


def _gpu_chip(gpu: Dict[str, Any]) -> None:
    """Render a single GPU chip: utilization + current/total VRAM (text only, no bars)."""
    util = gpu["util"]
    mem_used = gpu["mem_used"]
    mem_total = gpu["mem_total"]

    with ui.row().classes(
        "items-center gap-1.5 bg-white/5 px-2 py-0.5 rounded border border-white/10"
    ):
        ui.label(f"G{gpu['index']}").classes(
            "text-[9px] font-bold text-gray-400 font-mono"
        )
        ui.label(f"{util:.0f}%").classes("text-[10px] font-mono text-white/90")
        ui.label(f"{mem_used:.0f}/{mem_total:.0f}M").classes(
            "text-[9px] font-mono text-white/60"
        )


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

                    # ── GPU section (text only, no color bars) ──
                    gpus: List[Dict[str, Any]] = m.get("gpus") or []
                    n_gpus = len(gpus)

                    if n_gpus == 0:
                        pass
                    elif n_gpus <= 4:
                        for gpu in gpus:
                            _gpu_chip(gpu)
                    else:
                        # >4 GPUs: compact aggregate
                        avg_util = sum(g["util"] for g in gpus) / n_gpus
                        total_mem = sum(g["mem_total"] for g in gpus)
                        used_mem = sum(g["mem_used"] for g in gpus)
                        with ui.row().classes(
                            "items-center gap-2 bg-white/5 px-2.5 py-0.5 "
                            "rounded border border-white/10"
                        ):
                            ui.label(f"{n_gpus}×GPU").classes(
                                "text-[9px] font-bold text-gray-400 font-mono"
                            )
                            ui.label(f"{avg_util:.0f}%").classes(
                                "text-[10px] font-mono text-white/90"
                            )
                            ui.label(f"{used_mem:.0f}/{total_mem:.0f}M").classes(
                                "text-[9px] font-mono text-white/60"
                            )

                ui.timer(_METRICS_REFRESH_INTERVAL, metrics_row.refresh, once=True)

            metrics_row()
