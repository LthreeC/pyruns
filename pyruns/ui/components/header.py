"""
Header component — app branding + live CPU/RAM/GPU metrics.
"""
from nicegui import ui
from typing import Dict, Any, List

from pyruns._config import HEADER_GRADIENT
from pyruns.utils.settings import get as _get_setting


def _gpu_chip(gpu: Dict[str, Any]) -> None:
    """Render a compact GPU chip: index + utilization + VRAM."""
    with ui.row().classes(
        "items-center gap-1.5 bg-white/5 px-2 py-0.5 "
        "rounded border border-white/10"
    ):
        ui.label(f"G{gpu['index']}").classes(
            "text-[9px] font-bold text-gray-400 font-mono"
        )
        ui.label(f"{gpu['util']:.0f}%").classes(
            "text-[10px] font-mono text-white/90"
        )
        ui.label(f"{gpu['mem_used']:.0f}/{gpu['mem_total']:.0f}M").classes(
            "text-[9px] font-mono text-white/60"
        )


def render_header(state: Dict[str, Any], metrics_sampler) -> None:
    """Render the top header bar with branding and system metrics."""
    with ui.header().classes(
        f"{HEADER_GRADIENT} text-white px-6 py-2 shadow-md "
        "border-b border-white/10 items-center justify-between"
    ):
        # ── Branding ──
        with ui.row().classes("items-center gap-3"):
            ui.icon("rocket_launch", size="28px", color="white")
            ui.label("PYRUNS LAB").classes(
                "text-xl font-bold tracking-widest font-mono text-white/90"
            )

        # ── Live metrics ──
        with ui.row().classes("items-center gap-4"):

            @ui.refreshable
            def metrics_row() -> None:
                m = metrics_sampler.sample()

                with ui.row().classes(
                    "items-center gap-3 bg-white/5 px-3 py-1 "
                    "rounded-full border border-white/10 backdrop-blur-sm"
                ):
                    _stat_chip("CPU", f"{m['cpu_percent']:.0f}%", "memory")
                    _stat_chip("RAM", f"{m['mem_percent']:.0f}%", "memory")

                    gpus: List[Dict[str, Any]] = m.get("gpus") or []
                    for gpu in gpus:
                        _gpu_chip(gpu)

                # Schedule next refresh (interval from workspace settings)
                interval = _get_setting("header_refresh_interval", 3)
                ui.timer(interval, metrics_row.refresh, once=True)

            metrics_row()


def _stat_chip(label: str, value: str, icon_name: str) -> None:
    """Render a small CPU/RAM stat pill."""
    with ui.row().classes(
        "items-center gap-1 bg-white/5 px-2 py-0.5 "
        "rounded-full border border-white/10"
    ):
        ui.icon(icon_name, size="xs", color="gray-300")
        ui.label(label).classes(
            "text-[10px] font-bold text-gray-400 uppercase tracking-wider"
        )
        ui.label(value).classes("text-xs font-mono text-white")
