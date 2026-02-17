"""
Manager Page — task card grid with filters, batch controls, and polling.

Layout:
  Row 1: directory picker + status filter + search
  Row 2: columns selector (left) | workers + mode + run + delete (right)
  Body:  refreshable card grid with summary bar + pagination
"""
import copy
from nicegui import ui
from typing import Dict, Any

from pyruns.ui.theme import (
    INPUT_PROPS, BTN_CLASS, STATUS_ICONS, STATUS_ICON_COLORS,
)
from pyruns.ui.widgets import dir_picker, section_header
from pyruns.ui.components.task_card import render_card_grid
from pyruns.ui.components.task_dialog import build_task_dialog
from pyruns.utils.sort_utils import task_sort_key
from pyruns.utils import get_logger

logger = get_logger(__name__)

ALL_STATUSES = ["pending", "queued", "running", "completed", "failed"]

def render_manager_page(state: Dict[str, Any], task_manager) -> None:
    """Entry point for the Manager tab."""
    from pyruns.utils.settings import get as _get_setting

    # Maximum cards rendered per page (0 = show all)
    _PAGE_SIZE: int = int(_get_setting("manager_page_size", 50)) or 0

    selected = {"task": None, "tab": "task_info"}
    build_task_dialog(selected, state, task_manager)
    open_task_dialog = selected["_open_fn"]

    # ── Pagination state ──
    _page = {"value": 0}

    # ── Snapshot for smart polling ──
    _last_snap: Dict[str, tuple] = {}
    _needs_refresh = {"flag": False}

    def _take_snap() -> Dict[str, tuple]:
        return {
            t["id"]: (t["status"], t.get("progress", 0))
            for t in task_manager.tasks
        }

    def refresh_ui():
        """Re-render cards without disk I/O."""
        task_list.refresh()

    def refresh_tasks():
        """Sync from disk then re-render (used after user actions)."""
        nonlocal _last_snap
        task_manager.refresh_from_disk()
        _last_snap = _take_snap()
        task_list.refresh()

    def full_rescan():
        nonlocal _last_snap
        task_manager.scan_disk()
        _last_snap = _take_snap()
        _page["value"] = 0
        task_list.refresh()
        logger.debug("Full rescan completed, %d tasks", len(task_manager.tasks))

    def poll_changes():
        """Periodic: re-render only when the snapshot changes."""
        nonlocal _last_snap

        # Instant refresh when generator creates new tasks
        if state.get("_manager_dirty"):
            state["_manager_dirty"] = False
            task_manager.refresh_from_disk()
            _last_snap = _take_snap()
            task_list.refresh()
            return

        # Catch up immediately when switching back to this tab
        if _needs_refresh["flag"] and state.get("active_tab") == "manager":
            _needs_refresh["flag"] = False
            task_manager.refresh_from_disk()
            _last_snap = _take_snap()
            task_list.refresh()
            return

        task_manager.refresh_from_disk()
        snap = _take_snap()
        if snap != _last_snap:
            _last_snap = snap
            # Skip DOM rebuild when page is hidden — mark as stale so
            # we rebuild on the next poll cycle when the tab is active.
            if state.get("active_tab") == "manager":
                task_list.refresh()
            else:
                _needs_refresh["flag"] = True

    # ══════════════════════════════════════════════════════════
    #  Row 1 — Directory picker + Filter + Search
    # ══════════════════════════════════════════════════════════
    with ui.row().classes(
        "w-full items-center gap-2 mb-2 px-3 py-1.5 bg-white "
        "border-b border-slate-200 shadow-sm"
    ):
        tasks_dir_input = dir_picker(
            value=state.get("tasks_dir", task_manager.root_dir),
            label="Tasks Root",
            on_change=lambda p: _apply_dir(p, state, task_manager, full_rescan),
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

        ui.button(
            icon="refresh",
            on_click=lambda: _apply_dir(
                tasks_dir_input.value, state, task_manager, full_rescan,
            ),
        ).props("flat round dense color=slate")

    # ══════════════════════════════════════════════════════════
    #  Row 2 — [Cols] ←──────→ [Workers Mode RUN DELETE]
    # ══════════════════════════════════════════════════════════
    with ui.row().classes(
        "w-full items-center justify-between bg-white px-3 py-1.5 mb-2 "
        "border-b border-slate-200 shadow-sm"
    ):
        # Left: columns
        with ui.row().classes("items-center gap-1.5"):
            ui.icon("grid_view", size="18px").classes("text-slate-400")
            col_sel = ui.select(
                {n: f"{n} cols" for n in range(1, 10)},
                value=state.get("manager_columns", 5),
                label="Columns",
            ).props(INPUT_PROPS + " options-dense").classes("w-24")
            col_sel.on_value_change(
                lambda e: (
                    state.update({"manager_columns": int(e.value)}),
                    task_list.refresh(),
                )
            )

        # Right: workers + mode + run + delete
        with ui.row().classes("items-center gap-3"):
            w_input = ui.number(
                value=state["max_workers"], min=1, max=32, step=1,
                label="Workers",
            ).props(INPUT_PROPS).classes("w-20")
            w_input.on_value_change(
                lambda e: state.update({
                    "max_workers": int(e.value) if e.value else 1
                })
            )

            mode_sel = ui.select(
                {"thread": "Thread", "process": "Process"},
                value=state["execution_mode"], label="Mode",
            ).props(INPUT_PROPS + " options-dense").classes("w-28")
            mode_sel.on_value_change(
                lambda e: state.update({"execution_mode": e.value})
            )

            ui.button(
                "RUN SELECTED", icon="play_arrow",
                on_click=lambda: _run_selected(state, task_manager, refresh_tasks),
            ).props("unelevated no-caps color=green-8").classes(
                f"font-bold px-6 py-2.5 text-white text-sm "
                f"shadow-md {BTN_CLASS}"
            )

            ui.button(
                icon="delete_outline",
                on_click=lambda: _delete_selected(state, task_manager, refresh_tasks),
            ).props("unelevated dense color=red-7").classes(
                f"text-white {BTN_CLASS}"
            )

    # ══════════════════════════════════════════════════════════
    #  Card grid (refreshable) with pagination
    # ══════════════════════════════════════════════════════════

    def _reset_page_and_refresh():
        _page["value"] = 0
        task_list.refresh()

    @ui.refreshable
    def task_list() -> None:
        tasks = _filter_tasks(
            task_manager.tasks,
            filter_status.value,
            (search_input.value or "").strip().lower(),
        )

        if not tasks:
            _page["value"] = 0
            _empty_state()
            return

        # Combine pinned-first then others, sort each group by latest activity
        pinned = sorted(
            [t for t in tasks if t.get("pinned")],
            key=task_sort_key, reverse=True,
        )
        others = sorted(
            [t for t in tasks if not t.get("pinned")],
            key=task_sort_key, reverse=True,
        )
        ordered = pinned + others

        total = len(ordered)
        if _PAGE_SIZE > 0:
            total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
            page = min(_page["value"], total_pages - 1)
            _page["value"] = page
            start = page * _PAGE_SIZE
            end = min(start + _PAGE_SIZE, total)
            page_slice = ordered[start:end]
        else:
            total_pages = 1
            page_slice = ordered

        _summary_bar(tasks, state, task_list, _page, total_pages)

        # Split the page slice back into pinned / non-pinned groups
        p_pinned = [t for t in page_slice if t.get("pinned")]
        p_others = [t for t in page_slice if not t.get("pinned")]

        if p_pinned:
            section_header("Pinned", icon="push_pin", extra_classes="mt-4 mb-1")
            render_card_grid(
                p_pinned, state, task_manager,
                open_task_dialog, refresh_tasks, refresh_ui,
            )
        if p_others:
            if p_pinned:
                section_header(
                    "All Tasks", icon="list", extra_classes="mt-4 mb-1",
                )
            render_card_grid(
                p_others, state, task_manager,
                open_task_dialog, refresh_tasks, refresh_ui,
            )

    filter_status.on_value_change(lambda _: _reset_page_and_refresh())
    search_input.on_value_change(lambda _: _reset_page_and_refresh())

    # Render immediately — tasks already in memory (TaskManager.__init__ ran
    # scan_disk).  No deferred timer needed; this avoids a costly double render.
    task_list()
    _last_snap = _take_snap()

    # Periodic polling (from workspace settings)
    ui.timer(float(_get_setting("manager_poll_interval", 2)), poll_changes)


# ═══════════════════════════════════════════════════════════
#  Page-level helpers
# ═══════════════════════════════════════════════════════════

def _apply_dir(path: str, state, task_manager, full_rescan) -> None:
    logger.info("Switching tasks root to: %s", path)
    state["tasks_dir"] = path
    task_manager.root_dir = path
    full_rescan()


def _run_selected(state, task_manager, refresh_tasks) -> None:
    ids = state.get("selected_task_ids", [])
    if not ids:
        ui.notify("No tasks selected", type="warning", icon="warning")
        return
    task_manager.start_batch_tasks(
        ids, state["execution_mode"], state["max_workers"],
    )
    logger.info("Started %d task(s)  mode=%s  workers=%d",
                len(ids), state["execution_mode"], state["max_workers"])
    ui.notify(
        f"Started {len(ids)} task(s)", type="positive", icon="play_arrow",
    )
    refresh_tasks()


def _delete_selected(state, task_manager, refresh_tasks) -> None:
    ids = copy.copy(state.get("selected_task_ids", []))
    if not ids:
        return
    for tid in ids:
        task_manager.delete_task(tid)
    state["selected_task_ids"] = []
    logger.info("Deleted %d task(s) to trash", len(ids))
    ui.notify(
        f"Moved {len(ids)} task(s) to trash", type="warning", icon="delete",
    )
    refresh_tasks()


def _filter_tasks(all_tasks, mode, query):
    """Apply status + search filters."""
    tasks = [
        t for t in all_tasks
        if mode == "All" or mode.lower() == t["status"]
    ]
    if query:
        tasks = [t for t in tasks if query in t.get("name", "").lower()]
    return tasks


def _empty_state() -> None:
    with ui.column().classes("w-full items-center justify-center py-20"):
        ui.icon("inbox", size="72px").classes("text-slate-200 mb-3")
        ui.label("No tasks found").classes("text-xl font-bold text-slate-400")
        ui.label(
            "Generate tasks from the Generator page, or change the filter."
        ).classes("text-sm text-slate-400 mt-1")


def _summary_bar(
    visible_tasks, state, task_list_refreshable,
    page_state=None, total_pages=1,
) -> None:
    with ui.row().classes(
        "w-full px-3 py-1.5 bg-white border-b border-slate-200 "
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

            ui.checkbox(
                value=all_selected, on_change=toggle_all,
            ).props("dense color=indigo")
            cnt = len(visible_tasks)
            ui.label(f"{cnt} task{'s' if cnt != 1 else ''}").classes(
                "text-xs font-bold text-slate-500 uppercase tracking-wider"
            )

        with ui.row().classes("items-center gap-3"):
            counts: Dict[str, int] = {}
            for t in visible_tasks:
                s = t["status"]
                counts[s] = counts.get(s, 0) + 1

            for s, c in counts.items():
                icon_cls = STATUS_ICON_COLORS.get(s, "text-slate-400")
                icon_name = STATUS_ICONS.get(s, "help")
                with ui.row().classes(
                    "items-center gap-1 bg-slate-50 px-2 py-0.5"
                ):
                    ui.icon(icon_name, size="13px").classes(icon_cls)
                    ui.label(str(c)).classes(
                        f"text-[11px] font-bold {icon_cls}"
                    )

            # ── Pagination controls (only when multiple pages) ──
            if page_state is not None and total_pages > 1:
                cur = page_state["value"]

                def _prev():
                    if page_state["value"] > 0:
                        page_state["value"] -= 1
                        task_list_refreshable.refresh()

                def _next():
                    if page_state["value"] < total_pages - 1:
                        page_state["value"] += 1
                        task_list_refreshable.refresh()

                with ui.row().classes(
                    "items-center gap-1 ml-2 pl-2 border-l border-slate-200"
                ):
                    b_prev = ui.button(icon="chevron_left", on_click=_prev).props(
                        "flat dense round size=sm"
                    ).classes("text-slate-500")
                    if cur == 0:
                        b_prev.props(add="disable")

                    ui.label(f"{cur + 1}/{total_pages}").classes(
                        "text-[11px] font-mono font-bold text-slate-500 min-w-[2rem] text-center"
                    )

                    b_next = ui.button(icon="chevron_right", on_click=_next).props(
                        "flat dense round size=sm"
                    ).classes("text-slate-500")
                    if cur >= total_pages - 1:
                        b_next.props(add="disable")
