"""
Task Card component – renders a single task card in the manager grid.
"""
from nicegui import ui
from typing import Dict, Any, Callable

from pyruns.utils.config_utils import preview_config_line
from pyruns.utils.info_io import load_task_info, save_task_info
from pyruns.ui.theme import (
    BTN_CLASS,
    STATUS_CARD_STYLES,
    MANAGER_GRID_CLASSES, CARD_BASE_CLASSES, CARD_HEADER_CLASSES, CARD_TITLE_COL_CLASSES,
    CARD_TITLE_CLASSES, CARD_TIME_CLASSES, PIN_ACTIVE_CLASSES, PIN_INACTIVE_CLASSES,
    CARD_BADGE_ROW_CLASSES, CARD_BODY_CLASSES, CARD_CONFIG_LINE_CLASSES, CARD_FOOTER_CLASSES,
    CHECKBOX_PROPS, ACTION_BTN_PROPS, ACTION_BTN_CLASSES,
    TASK_CARD_ERROR_CLASSES, TASK_CARD_CHECKBOX_CLASSES,
    TASK_CARD_META_ROW_CLASSES, TASK_CARD_META_ICON_CLASSES,
    TASK_CARD_META_TEXT_CLASSES, TASK_CARD_ACTIONS_LEFT_CLASSES,
    TASK_CARD_RUNNING_ACTIONS_CLASSES,
)
from pyruns.ui.widgets import status_badge


def render_card_grid(
    tasks, state: Dict[str, Any], task_manager,
    open_task_dialog: Callable, refresh_tasks: Callable, refresh_ui: Callable,
):
    try:
        with ui.grid(columns=state.get("manager_columns", 4)).classes(MANAGER_GRID_CLASSES):
            for t in tasks:
                render_task_card(t, state, task_manager, open_task_dialog, refresh_tasks, refresh_ui)
    except Exception as e:
        import traceback
        traceback.print_exc()
        ui.label(f"Render Error: {e}").classes(TASK_CARD_ERROR_CLASSES)


def render_task_card(
    t: Dict[str, Any], state: Dict[str, Any], task_manager,
    open_task_dialog: Callable, refresh_tasks: Callable, refresh_ui: Callable,
):
    """Render one task card."""
    status = t.get("status", "pending")
    card_style = STATUS_CARD_STYLES.get(status, "border-slate-200 bg-white")
    is_selected = t["name"] in state.get("selected_task_ids", [])
    highlight = "ring-2 ring-indigo-400 ring-offset-1" if is_selected else ""

    card = ui.card().classes(
        f"{CARD_BASE_CLASSES} {card_style} {highlight}"
    ).style("min-height: 148px")

    card.on("click", lambda _e=None, t=t: open_task_dialog(t))

    with card:
        # ── Header row: checkbox + name + pin ──
        with ui.row().classes(CARD_HEADER_CLASSES):
            def on_check(e, tid=t["name"]):
                is_checked = e.value
                if is_checked:
                    if tid not in state["selected_task_ids"]:
                        state["selected_task_ids"].append(tid)
                    card.classes(add="ring-2 ring-indigo-400 ring-offset-1")
                else:
                    if tid in state["selected_task_ids"]:
                        state["selected_task_ids"].remove(tid)
                    card.classes(remove="ring-2 ring-indigo-400 ring-offset-1")

            cb = ui.checkbox(value=is_selected, on_change=on_check).props(
                CHECKBOX_PROPS
            ).classes(TASK_CARD_CHECKBOX_CLASSES).on("click", js_handler="(e) => e.stopPropagation()")
            
            state.setdefault("_manager_checkboxes", {})[t["name"]] = cb

            with ui.column().classes(CARD_TITLE_COL_CLASSES):
                ui.label(t["name"]).classes(CARD_TITLE_CLASSES).tooltip(t["name"])
                ui.label(t.get("created_at", "")).classes(CARD_TIME_CLASSES)

            def toggle_pin(t=t):
                info = load_task_info(t["dir"])
                pinned = not info.get("pinned", False)
                info["pinned"] = pinned
                save_task_info(t["dir"], info)
                t["pinned"] = pinned
                refresh_tasks()

            pin_cls = (
                PIN_ACTIVE_CLASSES
                if t.get("pinned")
                else PIN_INACTIVE_CLASSES
            )
            ui.button(icon="push_pin", on_click=toggle_pin).props(
                "flat round dense size=xs"
            ).classes(
                f"{pin_cls} transition-opacity duration-200 mt-0.5"
            ).on("click", js_handler="(e) => e.stopPropagation()")

        # ── Status badge ──
        with ui.row().classes(CARD_BADGE_ROW_CLASSES):
            status_badge(status, size="sm")

        # ── Config preview + run count ──
        with ui.column().classes(CARD_BODY_CLASSES):
            line = preview_config_line(t.get("config", {}))
            ui.label(line if line else "\u2014").classes(CARD_CONFIG_LINE_CLASSES)

            starts = t.get("start_times") or []
            run_count = len(starts)
            if run_count > 1:
                with ui.row().classes(TASK_CARD_META_ROW_CLASSES):
                    ui.icon("replay", size="12px").classes(TASK_CARD_META_ICON_CLASSES)
                    ui.label(f"{run_count} run(s)").classes(
                        TASK_CARD_META_TEXT_CLASSES
                    )

        # ── Bottom action bar ──
        with ui.row().classes(CARD_FOOTER_CLASSES).on("click", js_handler="(e) => e.stopPropagation()"):
            with ui.row().classes(TASK_CARD_ACTIONS_LEFT_CLASSES):
                _card_action_btn(
                    icon="description", tooltip="Task Info",
                    on_click=lambda _e=None, t=t: open_task_dialog(t, "task_info"),
                )
                _card_action_btn(
                    icon="tune", tooltip="Config",
                    on_click=lambda _e=None, t=t: open_task_dialog(t, "config"),
                )
                _card_action_btn(
                    icon="edit_note", tooltip="Notes",
                    on_click=lambda _e=None, t=t: open_task_dialog(t, "notes"),
                )


            _card_run_indicator(t, status, state, task_manager)


# ── Small helpers ──

def _card_action_btn(icon: str, tooltip: str, on_click: Callable):
    ui.button(icon=icon, on_click=on_click).props(ACTION_BTN_PROPS).classes(ACTION_BTN_CLASSES).tooltip(tooltip)


def _card_run_indicator(t, status, state, task_manager):
    tid = t["name"]
    if status in ("pending", "failed"):
        def run_single(*args):
            task_manager.start_task_now(tid, state.get("execution_mode", "thread"))
            ui.notify("Started task instantly", type="positive", icon="play_arrow")
            # trigger_update() in start_task_now already fires observer callbacks.

        ui.button("RUN", icon="play_arrow", on_click=run_single).props(
            "unelevated no-caps dense size=sm"
        ).classes(
            f"text-white text-[11px] px-3 bg-emerald-600 hover:bg-emerald-700 "
            f"shadow-sm {BTN_CLASS}"
        ).on("click", js_handler="(e) => e.stopPropagation()")

    elif status in ("running", "queued"):
        def cancel_single(*args):
            ok = task_manager.cancel_task(tid)
            if ok:
                ui.notify("Task stopped", type="warning", icon="stop")
            else:
                ui.notify("Cannot cancel this task", type="negative")

        with ui.row().classes(TASK_CARD_RUNNING_ACTIONS_CLASSES):
            if status == "running":
                ui.spinner("dots", size="14px", color="amber")
            ui.button("STOP", icon="stop", on_click=cancel_single).props(
                "unelevated no-caps dense size=sm"
            ).classes(
                f"text-white text-[11px] px-3 bg-rose-600 hover:bg-rose-700 "
                f"shadow-sm {BTN_CLASS}"
            ).on("click", js_handler="(e) => e.stopPropagation()")

    elif status == "completed":
        def rerun_single(*args):
            ok = task_manager.rerun_task(tid)
            if ok:
                ui.notify("Rerun started", type="positive", icon="replay")
            else:
                ui.notify("Cannot rerun this task", type="negative")

        with ui.row().classes(TASK_CARD_RUNNING_ACTIONS_CLASSES):
            ui.icon("check_circle", size="20px").classes("text-emerald-500")
            from pyruns.ui.theme import BTN_PRIMARY
            ui.button("RERUN", icon="replay", on_click=rerun_single).props(
                "unelevated no-caps dense size=sm"
            ).classes(
                f"{BTN_PRIMARY} px-3 text-[11px]"
            ).on("click", js_handler="(e) => e.stopPropagation()")

