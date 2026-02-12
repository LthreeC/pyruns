"""
Manager Page – task card grid with filters, controls, and polling.
"""
import copy
from nicegui import ui
from typing import Dict, Any

from pyruns.ui.theme import INPUT_PROPS, BTN_CLASS, STATUS_ICONS, STATUS_ICON_COLORS
from pyruns.ui.widgets import dir_picker, section_header
from pyruns.ui.components.task_card import render_card_grid
from pyruns.ui.components.task_dialog import build_task_dialog
from pyruns.utils import get_logger

logger = get_logger(__name__)

ALL_STATUSES = ["pending", "queued", "running", "completed", "failed"]


def render_manager_page(state: Dict[str, Any], task_manager) -> None:
    """Page 2: Task Manager — beautiful card grid with filters."""

    selected = {"task": None, "tab": "task_info"}

    build_task_dialog(selected, state, task_manager)
    open_task_dialog = selected["_open_fn"]

    # ── Refresh helpers ──
    _last_ui_snap: Dict[str, tuple] = {}

    def refresh_ui():
        """Lightweight: just re-render cards (no disk I/O)."""
        task_list.refresh()

    def refresh_tasks():
        """Full: sync from disk then re-render. Used after user actions."""
        nonlocal _last_ui_snap
        task_manager.refresh_from_disk()
        _last_ui_snap = {t["id"]: (t["status"], t.get("progress", 0)) for t in task_manager.tasks}
        task_list.refresh()

    def full_rescan():
        nonlocal _last_ui_snap
        task_manager.scan_disk()
        _last_ui_snap = {t["id"]: (t["status"], t.get("progress", 0)) for t in task_manager.tasks}
        task_list.refresh()

    def poll_disk_changes():
        """Periodic: sync from disk, re-render only when something changed."""
        nonlocal _last_ui_snap
        task_manager.refresh_from_disk()
        new_snap = {t["id"]: (t["status"], t.get("progress", 0)) for t in task_manager.tasks}
        if new_snap != _last_ui_snap:
            _last_ui_snap = new_snap
            task_list.refresh()

    # ══════════════════════════════════════════════════════════════
    #  Row 1: Directory picker + Filter
    # ══════════════════════════════════════════════════════════════
    with ui.row().classes(
        "w-full items-center gap-4 mb-4 bg-white p-4 rounded-xl shadow-sm border border-slate-100"
    ):
        tasks_dir_input = dir_picker(
            value=state.get("tasks_dir", task_manager.root_dir),
            label="Tasks Root",
            on_change=lambda path: _apply_dir(path, state, task_manager, full_rescan),
            input_classes="flex-grow",
        )

        filter_status = ui.select(
            {"All": "All", **{s: s.capitalize() for s in ALL_STATUSES}},
            value="All", label="Filter",
        ).props(INPUT_PROPS).classes("w-48")

        search_input = ui.input(
            value="", label="Search", placeholder="Task name...",
        ).props(INPUT_PROPS + " clearable").classes("w-56")
        search_input.on("keyup.enter", lambda _: task_list.refresh())
        search_input.on("clear", lambda _: task_list.refresh())

        ui.button(icon="refresh", on_click=lambda: _apply_dir(
            tasks_dir_input.value, state, task_manager, full_rescan
        )).props("flat round dense color=slate")

    # ══════════════════════════════════════════════════════════════
    #  Row 2: Execution controls + Batch actions
    # ══════════════════════════════════════════════════════════════
    with ui.row().classes(
        "w-full items-center justify-between bg-white px-5 py-3 mb-4 "
        "rounded-xl shadow-sm border border-slate-100"
    ):
        with ui.row().classes("items-center gap-4"):
            w_input = ui.number(
                value=state["max_workers"], min=1, max=32, step=1, label="Workers",
            ).props(INPUT_PROPS).classes("w-28")
            w_input.on_value_change(
                lambda e: state.update({"max_workers": int(e.value) if e.value else 1})
            )

            mode_sel = ui.select(
                {"thread": "Thread Pool", "process": "Process Pool"},
                value=state["execution_mode"], label="Mode",
            ).props(INPUT_PROPS + " options-dense").classes("w-36")
            mode_sel.on_value_change(lambda e: state.update({"execution_mode": e.value}))

            col_sel = ui.select(
                list(range(1, 10)), value=state.get("manager_columns", 4), label="Columns",
            ).props(INPUT_PROPS + " options-dense").classes("w-28")
            col_sel.on_value_change(
                lambda e: (state.update({"manager_columns": int(e.value)}), task_list.refresh())
            )

        with ui.row().classes("items-center gap-3"):
            def run_selected():
                ids = state.get("selected_task_ids", [])
                if not ids:
                    ui.notify("No tasks selected", type="warning", icon="warning")
                    return
                task_manager.start_batch_tasks(ids, state["execution_mode"], state["max_workers"])
                ui.notify(f"Started {len(ids)} task(s)", type="positive", icon="play_arrow")
                refresh_tasks()

            ui.button("RUN", icon="play_arrow", on_click=run_selected).props(
                "unelevated no-caps"
            ).classes(
                f"font-bold px-5 py-2 text-white bg-emerald-600 hover:bg-emerald-700 "
                f"shadow-sm rounded-lg {BTN_CLASS}"
            )

            def delete_selected():
                ids = copy.copy(state.get("selected_task_ids", []))
                if not ids:
                    return
                for tid in ids:
                    task_manager.delete_task(tid)
                state["selected_task_ids"] = []
                ui.notify(f"Moved {len(ids)} task(s) to trash", type="warning", icon="delete")
                refresh_tasks()

            ui.button(icon="delete_outline", on_click=delete_selected).props(
                "flat round dense"
            ).classes("text-rose-400 hover:text-rose-600 hover:bg-rose-50")

    # ══════════════════════════════════════════════════════════════
    #  Task Card Grid (refreshable)
    # ══════════════════════════════════════════════════════════════
    @ui.refreshable
    def task_list() -> None:
        all_tasks = task_manager.tasks
        mode = filter_status.value
        query = (search_input.value or "").strip().lower()
        logger.info(f"[ManagerPage] Refreshing. Total={len(all_tasks)}, Filter={mode}, Search='{query}'")

        # 1. 状态过滤
        visible_tasks = [
            t for t in all_tasks if mode == "All" or mode.lower() == t["status"]
        ]
        # 2. 搜索过滤 (按任务名模糊匹配)
        if query:
            visible_tasks = [
                t for t in visible_tasks if query in t.get("name", "").lower()
            ]

        if not visible_tasks:
            _render_empty_state()
            return

        _render_summary_bar(visible_tasks, state, task_list)

        pinned = [t for t in visible_tasks if t.get("pinned")]
        others = [t for t in visible_tasks if not t.get("pinned")]

        if pinned:
            section_header("Pinned", icon="push_pin", extra_classes="mt-4 mb-1")
            render_card_grid(pinned, state, task_manager, open_task_dialog, refresh_tasks, refresh_ui)
        if others:
            if pinned:
                section_header("All Tasks", icon="list", extra_classes="mt-4 mb-1")
            render_card_grid(others, state, task_manager, open_task_dialog, refresh_tasks, refresh_ui)

    filter_status.on_value_change(lambda _: task_list.refresh())
    search_input.on_value_change(lambda _: task_list.refresh())

    # Only full-scan when tasks list is empty (first load); otherwise quick refresh
    if not task_manager.tasks:
        logger.info("[ManagerPage] First load – full scan...")
        task_manager.scan_disk()
    else:
        logger.info("[ManagerPage] Tab switch – quick refresh...")
        task_manager.refresh_from_disk()
    task_list()

    # Periodic polling (2s)
    ui.timer(2.0, poll_disk_changes)


# ═══════════════════════════════════════════════════════════════
#  Page-level helper functions
# ═══════════════════════════════════════════════════════════════

def _apply_dir(path: str, state, task_manager, full_rescan):
    state["tasks_dir"] = path
    task_manager.root_dir = path
    full_rescan()


def _render_empty_state():
    with ui.column().classes("w-full items-center justify-center py-20"):
        ui.icon("inbox", size="72px").classes("text-slate-200 mb-3")
        ui.label("No tasks found").classes("text-xl font-bold text-slate-400")
        ui.label(
            "Generate tasks from the Generator page, or change the filter."
        ).classes("text-sm text-slate-400 mt-1")


def _render_summary_bar(visible_tasks, state, task_list_refreshable):
    with ui.row().classes(
        "w-full px-4 py-2 bg-white rounded-xl border border-slate-100 "
        "items-center justify-between shadow-sm"
    ):
        with ui.row().classes("items-center gap-3"):
            all_selected = (
                len(state.get("selected_task_ids", [])) == len(visible_tasks)
                and len(visible_tasks) > 0
            )

            def toggle_all(e):
                state["selected_task_ids"] = (
                    [t["id"] for t in visible_tasks] if e.value else []
                )
                task_list_refreshable.refresh()

            ui.checkbox(value=all_selected, on_change=toggle_all).props("dense color=indigo")
            cnt = len(visible_tasks)
            ui.label(f"{cnt} task{'s' if cnt != 1 else ''}").classes(
                "text-xs font-bold text-slate-500 uppercase tracking-wider"
            )

        with ui.row().classes("items-center gap-3"):
            status_counts: Dict[str, int] = {}
            for t in visible_tasks:
                s = t["status"]
                status_counts[s] = status_counts.get(s, 0) + 1

            for s, cnt in status_counts.items():
                icon_cls = STATUS_ICON_COLORS.get(s, "text-slate-400")
                icon_name = STATUS_ICONS.get(s, "help")
                with ui.row().classes("items-center gap-1 bg-slate-50 px-2 py-0.5 rounded-full"):
                    ui.icon(icon_name, size="13px").classes(icon_cls)
                    ui.label(f"{cnt}").classes(f"text-[11px] font-bold {icon_cls}")
