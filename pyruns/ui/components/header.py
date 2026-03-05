"""
Header component: app branding and live CPU/RAM/GPU summary.
"""
from typing import Any, Dict, List

from nicegui import ui

from pyruns.ui.theme import HEADER_GRADIENT
from pyruns.ui.theme import ROW_CENTER_GAP_3, ROW_CENTER_GAP_4
from pyruns.utils import client_connected
from pyruns.utils.settings import get as _get_setting, save_setting


def render_header(state: Dict[str, Any], metrics_sampler) -> None:
    """Render the top header bar with branding and system metrics."""
    with ui.header().classes(
        f"{HEADER_GRADIENT} text-white px-6 py-2 shadow-md "
        "border-b border-white/10 items-center justify-between"
    ):
        with ui.row().classes(ROW_CENTER_GAP_3):
            ui.icon("rocket_launch", size="28px", color="white")
            ui.label("PYRUNS LAB").classes(
                "text-xl font-bold tracking-widest font-mono text-white/90"
            )

        with ui.row().classes(ROW_CENTER_GAP_4):
            interval_state = {
                "sec": max(1, int(_get_setting("header_refresh_interval", 3)))
            }

            @ui.refreshable
            def metrics_row() -> None:
                if not client_connected():
                    return
                m = metrics_sampler.sample()

                with ui.row().classes(
                    "items-center gap-3 bg-white/5 px-3 py-1 "
                    "border border-white/10 backdrop-blur-sm"
                ):
                    _stat_chip("CPU", f"{m['cpu_percent']:.0f}%", "memory")
                    _stat_chip("RAM", f"{m['mem_percent']:.0f}%", "memory")

                    gpus: List[Dict[str, Any]] = m.get("gpus") or []
                    if gpus:
                        avg_util = sum(g["util"] for g in gpus) / len(gpus)
                        _stat_chip(
                            f"GPU {len(gpus)}x",
                            f"{avg_util:.0f}%",
                            "developer_board",
                        )

            metrics_row()
            timer_ref = {"obj": ui.timer(interval_state["sec"], metrics_row.refresh)}

            def _apply_refresh_interval(e) -> None:
                sec = max(1, int(e.value or 1))
                interval_state["sec"] = sec
                save_setting("header_refresh_interval", sec)
                if timer_ref["obj"] is not None:
                    timer_ref["obj"].cancel()
                timer_ref["obj"] = ui.timer(sec, metrics_row.refresh)

            ui.number(
                value=interval_state["sec"],
                min=1,
                step=1,
                label="Refresh(s)",
                on_change=_apply_refresh_interval,
            ).props("dense dark outlined").classes("w-24 text-xs")


def _stat_chip(label: str, value: str, icon_name: str) -> None:
    with ui.row().classes(
        "items-center gap-1 bg-white/5 px-2 py-0.5 border border-white/10"
    ):
        ui.icon(icon_name, size="xs", color="gray-300")
        ui.label(label).classes(
            "text-[10px] font-bold text-gray-400 uppercase tracking-wider"
        )
        ui.label(value).classes("text-xs font-mono text-white")
