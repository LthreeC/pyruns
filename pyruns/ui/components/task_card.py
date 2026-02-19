"""
Task Card component – renders a single task card in the manager grid.
"""
from nicegui import ui
from typing import Dict, Any, Callable

from pyruns.utils.config_utils import preview_config_line
from pyruns.utils.task_io import load_task_info, save_task_info
from pyruns.ui.theme import (
    BTN_CLASS,
    STATUS_CARD_STYLES, STATUS_ICONS,
)
from pyruns.ui.widgets import status_badge


def render_card_grid(
    tasks, state: Dict[str, Any], task_manager,
    open_task_dialog: Callable, refresh_tasks: Callable, refresh_ui: Callable,
):
    """Render a grid of task cards."""
    try:
        with ui.grid(columns=state.get("manager_columns", 4)).classes("w-full pl-2 gap-3 mt-1"):
            for t in tasks:
                render_task_card(t, state, task_manager, open_task_dialog, refresh_tasks, refresh_ui)
    except Exception as e:
        import traceback
        traceback.print_exc()
        ui.label(f"Render Error: {e}").classes("text-red-500 font-bold p-4")


def render_task_card(
    t: Dict[str, Any], state: Dict[str, Any], task_manager,
    open_task_dialog: Callable, refresh_tasks: Callable, refresh_ui: Callable,
):
    """Render one task card."""
    status = t.get("status", "pending")
    card_style = STATUS_CARD_STYLES.get(status, "border-slate-200 bg-white")
    is_selected = t["id"] in state.get("selected_task_ids", [])
    highlight = "ring-2 ring-indigo-400 ring-offset-1" if is_selected else ""

    card = ui.card().classes(
        f"w-full border {card_style} {highlight} shadow-sm "
        f"hover:shadow-lg transition-all duration-200 cursor-pointer group p-0 overflow-hidden"
    ).style("min-height: 148px")

    card.on("click", lambda _e=None, t=t: open_task_dialog(t))

    with card:
        # ── Header row: checkbox + name + pin ──
        with ui.row().classes("w-full items-start px-3 pt-3 pb-1 gap-2"):
            def on_check(e, tid=t["id"]):
                if e.value:
                    if tid not in state["selected_task_ids"]:
                        state["selected_task_ids"].append(tid)
                else:
                    if tid in state["selected_task_ids"]:
                        state["selected_task_ids"].remove(tid)
                refresh_ui()

            ui.checkbox(value=is_selected, on_change=on_check).props(
                "dense color=indigo size=xs"
            ).classes("mt-0.5").on("click", js_handler="(e) => e.stopPropagation()")

            with ui.column().classes("flex-grow gap-0.5 min-w-0"):
                ui.label(t["name"]).classes(
                    "font-bold text-[13px] text-slate-800 group-hover:text-indigo-700 "
                    "transition-colors truncate leading-snug"
                )
                ui.label(t.get("created_at", "")).classes(
                    "text-[10px] text-slate-400 font-mono leading-tight"
                )

            def toggle_pin(t=t):
                info = load_task_info(t["dir"])
                pinned = not info.get("pinned", False)
                info["pinned"] = pinned
                save_task_info(t["dir"], info)
                t["pinned"] = pinned
                refresh_tasks()

            pin_cls = (
                "text-amber-500 opacity-100"
                if t.get("pinned")
                else "text-slate-300 opacity-0 group-hover:opacity-60"
            )
            ui.button(icon="push_pin", on_click=toggle_pin).props(
                "flat round dense size=xs"
            ).classes(
                f"{pin_cls} transition-opacity duration-200 mt-0.5"
            ).on("click", js_handler="(e) => e.stopPropagation()")

        # ── Status badge ──
        with ui.row().classes("w-full px-3 py-0.5"):
            status_badge(status, size="sm")

        # ── Config preview + run count ──
        with ui.column().classes("w-full px-3 py-1.5 gap-0.5 flex-grow"):
            line = preview_config_line(t.get("config", {}))
            ui.label(line if line else "\u2014").classes(
                "text-[11px] text-slate-500 truncate w-full font-mono leading-relaxed"
            )

            starts = t.get("start_times") or []
            run_count = len(starts)
            if run_count > 1:
                with ui.row().classes("items-center gap-1 mt-0.5"):
                    ui.icon("replay", size="12px").classes("text-indigo-400")
                    ui.label(f"{run_count} run(s)").classes(
                        "text-[10px] font-mono text-indigo-400"
                    )

        # ── Bottom action bar ──
        with ui.row().classes(
            "w-full items-center justify-between px-3 py-2 mt-auto "
            "border-t border-slate-100 bg-slate-50/60"
        ).on("click", js_handler="(e) => e.stopPropagation()"):
            with ui.row().classes("items-center gap-1"):
                _card_action_btn(
                    icon="description", tooltip="Task Info",
                    on_click=lambda _e=None, t=t: open_task_dialog(t, "task_info"),
                )
                _card_action_btn(
                    icon="tune", tooltip="Config",
                    on_click=lambda _e=None, t=t: open_task_dialog(t, "config"),
                )
                _card_action_btn(
                    icon="terminal", tooltip="View Log",
                    on_click=lambda _e=None, t=t: open_task_dialog(t, "run.log"),
                )


            _card_run_indicator(t, status, state, task_manager, refresh_tasks)


# ── Small helpers ──

def _card_action_btn(icon: str, tooltip: str, on_click: Callable):
    ui.button(icon=icon, on_click=on_click).props(
        "flat round dense size=sm"
    ).classes(
        "text-slate-400 hover:text-indigo-600 hover:bg-indigo-50 transition-colors"
    ).tooltip(tooltip)


def _card_run_indicator(t, status, state, task_manager, refresh_tasks):
    if status in ("pending", "failed"):
        def run_single(tid=t["id"]):
            task_manager.start_batch_tasks([tid], state.get("execution_mode", "thread"), 1)
            ui.notify("Started 1 task", type="positive", icon="play_arrow")
            refresh_tasks()

        ui.button("RUN", icon="play_arrow", on_click=run_single).props(
            "unelevated no-caps dense size=sm"
        ).classes(
            f"text-white text-[11px] px-3 bg-emerald-600 hover:bg-emerald-700 "
            f"shadow-sm {BTN_CLASS}"
        )

    elif status in ("running", "queued"):
        def cancel_single(tid=t["id"]):
            ok = task_manager.cancel_task(tid)
            if ok:
                ui.notify("Task stopped", type="warning", icon="stop")
            else:
                ui.notify("Cannot cancel this task", type="negative")
            refresh_tasks()

        with ui.row().classes("items-center gap-1.5"):
            if status == "running":
                ui.spinner("dots", size="14px", color="amber")
            ui.button("STOP", icon="stop", on_click=cancel_single).props(
                "unelevated no-caps dense size=sm"
            ).classes(
                f"text-white text-[11px] px-3 bg-rose-600 hover:bg-rose-700 "
                f"shadow-sm {BTN_CLASS}"
            )

    elif status == "completed":
        def rerun_single(tid=t["id"]):
            ok = task_manager.rerun_task(tid)
            if ok:
                ui.notify("Rerun started", type="positive", icon="replay")
            else:
                ui.notify("Cannot rerun this task", type="negative")
            refresh_tasks()

        with ui.row().classes("items-center gap-1.5"):
            ui.icon("check_circle", size="20px").classes("text-emerald-500")
            ui.button("RERUN", icon="replay", on_click=rerun_single).props(
                "unelevated no-caps dense size=sm"
            ).classes(
                f"text-white text-[11px] px-3 bg-indigo-600 hover:bg-indigo-700 "
                f"shadow-sm {BTN_CLASS}"
            )

