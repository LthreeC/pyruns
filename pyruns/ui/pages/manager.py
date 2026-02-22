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
    MANAGER_SUMMARY_BAR_CLASSES, MANAGER_ACTION_ROW_CLASSES,
    MANAGER_FILTER_ROW_CLASSES, MANAGER_EMPTY_ICON_SIZE,
    MANAGER_EMPTY_ICON_CLASSES, MANAGER_EMPTY_TITLE_CLASSES,
    MANAGER_EMPTY_SUB_CLASSES
)
from pyruns.ui.widgets import dir_picker, section_header
from pyruns.ui.components.task_card import render_card_grid
from pyruns.ui.components.task_dialog import build_task_dialog
from pyruns.utils.sort_utils import task_sort_key, filter_tasks
from pyruns.utils import get_logger

logger = get_logger(__name__)

ALL_STATUSES = ["pending", "queued", "running", "completed", "failed"]

def render_manager_page(state: Dict[str, Any], task_manager) -> None:
    """Entry point for the Manager tab."""
    from pyruns.utils.settings import get as _get_setting

    # Maximum cards rendered per page (0 = show all)
    _PAGE_SIZE: int = int(_get_setting("ui_page_size", 50)) or 0

    selected = {"task": None, "tab": "task_info"}
    build_task_dialog(selected, state, task_manager)
    open_task_dialog = selected["_open_fn"]

    # ── Pagination state ──
    _page = {"value": 0}

    def refresh_ui():
        """Re-render cards without disk I/O."""
        task_list.refresh()

    def refresh_tasks():
        """Sync from disk then re-render (used after manual user actions)."""
        task_manager.refresh_from_disk()
        task_list.refresh()

    def full_rescan():
        """Full disk rescan to discover new task directories."""
        task_manager.scan_disk()
        _page["value"] = 0
        task_list.refresh()
        logger.debug("Full rescan completed, %d tasks", len(task_manager.tasks))

    # polling logic removed since task_manager now pushes updates via events

    # ══════════════════════════════════════════════════════════
    #  Row 1: Filter & Search
    # ══════════════════════════════════════════════════════════
    tasks_dir_input, filter_status, search_input = _render_filter_row(
        state, task_manager, full_rescan, lambda: task_list.refresh()
    )

    # ══════════════════════════════════════════════════════════
    #  Row 2: Batch Actions & View Settings
    # ══════════════════════════════════════════════════════════
    _render_action_row(state, task_manager, refresh_tasks, lambda: task_list.refresh())


    # ══════════════════════════════════════════════════════════
    #  Card grid (refreshable) with pagination
    # ══════════════════════════════════════════════════════════

    def _reset_page_and_refresh():
        _page["value"] = 0
        task_list.refresh()

    @ui.refreshable
    def task_list() -> None:
        if state.get("_manager_is_loading", False):
            with ui.column().classes("w-full items-center justify-center py-20 gap-4 mt-10"):
                ui.spinner("dots", size="3em", color="indigo", thickness=0.3)
                ui.label("Loading Tasks...").classes("text-slate-500 font-bold tracking-wider animate-pulse")
            return

        state["_manager_checkboxes"] = {}
        state["_manager_cards"] = {}
        # Apply filters
        # 1. Directory filter
        tasks = task_manager.tasks
        if tasks_dir_input.value:
            # Normalize paths for comparison to avoid backslash/slash issues
            import os
            td_val = os.path.normpath(tasks_dir_input.value)
            tasks = [t for t in tasks if os.path.normpath(str(t.get("dir", ""))).startswith(td_val)]

        # 2. & 3. Apply Search & Status filters
        tasks = filter_tasks(
            tasks, 
            search_input.value or "", 
            filter_status.value or "All"
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
            # ## mt-0, mb-0: 强制去除标题的上下外边距，以保证和卡片的紧凑距离
            section_header("Pinned", icon="push_pin", extra_classes="mt-0 mb-0")
            render_card_grid(
                p_pinned, state, task_manager,
                open_task_dialog, refresh_tasks, refresh_ui,
            )
        if p_others:
            if p_pinned:
                # ## mt-0, mb-0: 同样去除“All Tasks”标题的外边距
                section_header(
                    "All Tasks", icon="list", extra_classes="mt-0 mb-0",
                )
            render_card_grid(
                p_others, state, task_manager,
                open_task_dialog, refresh_tasks, refresh_ui,
            )

    filter_status.on_value_change(lambda _: _reset_page_and_refresh())
    search_input.on_value_change(lambda _: _reset_page_and_refresh())

    # Capture the specific client context immediately upon render to safely hook from bg thread.
    client = ui.context.client

    def _mark_dirty():
        state["_manager_dirty"] = True

    task_manager.on_change(_mark_dirty)

    def _check_dirty():
        # Only refresh if data is dirty AND user is actually looking at the manager tab
        if state.get("_manager_dirty", False) and state.get("active_tab") == "manager":
            state["_manager_dirty"] = False
            task_manager.refresh_from_disk(check_all=True)
            task_list.refresh()

    ui.timer(1.0, _check_dirty)

    # ── Static refresh: immediately update when user switches TO the manager tab ──
    # This prevents the visual delay of waiting up to 1 second for the next timer tick.
    async def _on_tab_switch(tab: str):
        if tab == "manager":
            if state.get("_manager_dirty", False):
                # Put UI in loading state to prevent flash of empty space
                state["_manager_is_loading"] = True
                task_list.refresh()
                
                import asyncio
                await asyncio.sleep(0.05)
                
                state["_manager_is_loading"] = False
                state["_manager_dirty"] = False
                task_manager.refresh_from_disk(check_all=True)
                task_list.refresh()

    cbs = state.setdefault("on_tab_change", [])
    cbs.append(_on_tab_switch)


    # ## px-1: 左右内边距1 (4px); pb-1: 底部内边距1; gap-0: 消除子元素（全选框和任务卡片）之间的默认16px大间距
    # ## flex-grow: 高度填满剩余空间; overflow-y-auto: 允许原生下拉滚动
    with ui.column().classes("w-full flex-grow px-1 pb-1 gap-1 overflow-y-auto").style("min-height: 0;"):
        task_list()


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
    ui.notify(f"Starting {len(ids)} tasks in background...", type="info", icon="hourglass_empty")

    # We call this sync, and it now delegates the loop to a background thread natively.
    task_manager.start_batch_tasks(
        ids, state["execution_mode"], state["max_workers"],
    )
    logger.info("Started %d task(s)  mode=%s  workers=%d",
                len(ids), state["execution_mode"], state["max_workers"])
    state["selected_task_ids"] = []
    refresh_tasks()


def _delete_selected(state, task_manager, refresh_tasks) -> None:
    ids = copy.copy(state.get("selected_task_ids", []))
    if not ids:
        return

    with ui.dialog() as dialog, ui.card().classes("p-6 w-96 rounded-2xl shadow-xl"):
        with ui.row().classes("items-center gap-4 w-full mb-4 outline-none"):
            ui.icon("delete_outline", size="28px").classes("text-red-500 bg-red-50 p-2 rounded-full")
            ui.label(f"Delete {len(ids)} Task{'s' if len(ids)>1 else ''}").classes("text-lg font-bold text-slate-800")
            
        ui.label(
            "Are you sure you want to move these tasks to the trash? "
            "This action can only be reversed manually from the .trash folder."
        ).classes("text-sm text-slate-600 mb-6 font-medium leading-relaxed")
        
        with ui.row().classes("w-full justify-end gap-3"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps").classes("text-slate-500 hover:bg-slate-100 font-bold")
            
            async def proceed():
                dialog.close()
                ui.notify(f"Moving {len(ids)} tasks to trash...", type="warning", icon="delete")
                
                # Clear visual selection immediately
                state["selected_task_ids"] = []
                refresh_tasks()
                
                # Offload to IO pool to avoid blocking the UI loop
                from nicegui import run
                await run.io_bound(task_manager.delete_tasks, ids)
                
                # Refresh again to reflect the removed tasks
                refresh_tasks()
                
            ui.button("Move to Trash", on_click=proceed).props("unelevated color=red no-caps").classes("font-bold shadow-md shadow-red-500/20")
            
    dialog.open()


def _empty_state() -> None:
    with ui.column().classes("w-full items-center justify-center py-20"):
        ui.icon("inbox", size=MANAGER_EMPTY_ICON_SIZE).classes(MANAGER_EMPTY_ICON_CLASSES)
        ui.label("No tasks found").classes(MANAGER_EMPTY_TITLE_CLASSES)
        ui.label(
            "Generate tasks from the Generator page, or change the filter."
        ).classes(MANAGER_EMPTY_SUB_CLASSES)


def _summary_bar(
    visible_tasks, state, task_list_refreshable,
    page_state=None, total_pages=1,
) -> None:
    # ## 布局由 theme.py 统一定义: MANAGER_SUMMARY_BAR_CLASSES 包含所有边距设计
    with ui.row().classes(MANAGER_SUMMARY_BAR_CLASSES):
        with ui.row().classes("items-center gap-3"):
            all_selected = (
                len(state.get("selected_task_ids", [])) == len(visible_tasks)
                and len(visible_tasks) > 0
            )

            def toggle_all(e):
                is_checked = e.value
                state["selected_task_ids"] = (
                    [t["name"] for t in visible_tasks] if is_checked else []
                )

                # Instantly update visible checkboxes & cards without re-rendering the list
                for cb in state.get("_manager_checkboxes", {}).values():
                    if cb and cb.value != is_checked:
                        cb.value = is_checked

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
                    "items-center gap-1 bg-slate-50 px-2"
                ):
                    ui.icon(icon_name, size="13px").classes(icon_cls)
                    ui.label(str(c)).classes(
                        f"text-[11px] font-bold {icon_cls}"
                    )

            # ── Pagination controls (only when multiple pages) ──
            if page_state is not None and total_pages > 1:
                from pyruns.ui.widgets import pagination_controls
                pagination_controls(
                    page_state=page_state,
                    total_pages=total_pages,
                    on_change=lambda: task_list_refreshable.refresh(),
                    container_classes="gap-2 ml-2 pl-4 border-l border-slate-200",
                    align="center",
                    full_width=False,
                    compact=False,
                )


# ═══════════════════════════════════════════════════════════
#  UI Components (Rows)
# ═══════════════════════════════════════════════════════════

def _render_filter_row(state, task_manager, full_rescan, refresh_fn):
    """Render the top bar: Directory Picker | Filter | Search."""
    # ## 布局由 theme.py 统一定义: MANAGER_FILTER_ROW_CLASSES 包含所有搜索栏边距
    with ui.row().classes(MANAGER_FILTER_ROW_CLASSES):
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

        search_input = ui.textarea(
            value="", label="Search", placeholder="device: null...",
        ).props(INPUT_PROPS + " clearable autogrow").classes("w-56 font-mono").style("max-height: 80px; overflow-y: auto;")

        # Using a slight debounce for textarea to avoid lag on multi-line paste
        search_timer = [None]
        def _debounced_search():
            refresh_fn()

        def _on_search(_):
            if search_timer[0]:
                search_timer[0].cancel()
            search_timer[0] = ui.timer(0.3, _debounced_search, once=True)

        # Bind refresh events
        # We listen to keyup.enter so linebreaks insert normally, but we also manually trigger search refresh.
        search_input.on("keyup.enter", lambda _: refresh_fn())
        search_input.on("clear", lambda _: refresh_fn())
        search_input.on_value_change(_on_search)

        ui.button(
            icon="refresh",
            on_click=lambda: _apply_dir(
                tasks_dir_input.value, state, task_manager, full_rescan,
            ),
        ).props("flat round dense color=slate")

        return tasks_dir_input, filter_status, search_input


def _render_action_row(state, task_manager, batch_refresh_fn, ui_refresh_fn):
    """Render the second bar: Columns | Workers | Mode | Run | Delete."""
    # ## 布局由 theme.py 统一定义: MANAGER_ACTION_ROW_CLASSES
    with ui.row().classes(MANAGER_ACTION_ROW_CLASSES):
        # Left: columns
        with ui.row().classes("items-center gap-1.5"):
            col_sel = ui.select(
                {n: f"{n} cols" for n in range(1, 10)},
                value=state.get("manager_columns", 5),
                label="Columns",
            ).props(INPUT_PROPS + " options-dense").classes("w-24")
            col_sel.on_value_change(
                lambda e: (
                    state.update({"manager_columns": int(e.value)}),
                    ui_refresh_fn(),
                )
            )

        # Right: workers + mode + run + delete
        with ui.row().classes("items-center gap-3"):
            w_input = ui.number(
                value=state["max_workers"], min=1, step=1,
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
                on_click=lambda: _run_selected(state, task_manager, batch_refresh_fn),
            ).props("unelevated no-caps color=green-8").classes(
                f"font-bold px-6 py-2.5 text-white text-sm "
                f"shadow-md {BTN_CLASS}"
            )

            ui.button(
                icon="delete_outline",
                on_click=lambda: _delete_selected(state, task_manager, batch_refresh_fn),
            ).props("unelevated dense color=red-7").classes(
                f"text-white {BTN_CLASS}"
            )
