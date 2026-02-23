"""
Monitor Page – real-time ANSI-colored log viewer + export reports.

Design:
  • Left panel : task list with search + export button at bottom
  • Right panel: persistent log viewer updated via LogEmitter push (no polling)
  • Running tasks: executor emits log chunks → _handle_live_log → term.write()
  • Historical tasks: safe_read_log file read on select
  • Uses explicit calc(100vh - Xpx) height to avoid flex-chain issues
    with NiceGUI / Quasar intermediate wrapper divs.
"""
import os
from typing import Dict, Any, Optional
from nicegui import ui

# Direct safe-read logic moved to utils/log_io.py for robustness
from pyruns.utils.info_io import get_log_options, resolve_log_path
from pyruns.utils.log_io import safe_read_log
from pyruns.utils.settings import get as get_setting
from pyruns.ui.theme import (
    STATUS_ICONS, STATUS_ICON_COLORS,
    PANEL_HEADER_INDIGO, PANEL_HEADER_DARK,
    MONITOR_PANEL_WIDTH, MONITOR_WORKSPACE_CLASSES,
    MONITOR_TERMINAL_COL_CLASSES, MONITOR_HEADER_HEIGHT_PX,
    MONITOR_EMPTY_COL_CLASSES, MONITOR_EMPTY_ICON_SIZE,
    MONITOR_EMPTY_ICON_CLASSES, MONITOR_EMPTY_TEXT_CLASSES
)
from pyruns.ui.widgets import _ensure_css
from pyruns.ui.components.export_dialog import show_export_dialog
from pyruns.utils.events import log_emitter

from pyruns.utils import get_logger

logger = get_logger(__name__)



# ═══════════════════════════════════════════════════════════════
#  Entrypoint
# ═══════════════════════════════════════════════════════════════

def render_monitor_page(state: Dict[str, Any], task_manager) -> None:
    _ensure_css()  # shared CSS includes .monitor-log-pre etc.

    # ── per-session state ──
    sel: Dict[str, Any] = {
        "task_id": None,
        "log_file_name": None,
        "auto_scroll": True,
        "export_ids": set(),
        "_log_offset": 0,
    }

    # ----------------------------------------------------------
    #  Helpers
    # ----------------------------------------------------------
    def _get_task(tid: Optional[str]) -> Optional[Dict[str, Any]]:
        if not tid:
            return None
        return next((t for t in task_manager.tasks if t["name"] == tid), None)

    def _current_log_path() -> Optional[str]:
        task = _get_task(sel["task_id"])
        if not task:
            return None
        return resolve_log_path(task["dir"], sel.get("log_file_name"))

    # ═════════════════════════════════════════════════════════
    #  Two-column layout — explicit height via calc()
    # ═════════════════════════════════════════════════════════
    with ui.row().classes(MONITOR_WORKSPACE_CLASSES).style(
        f"height: calc(100vh - {MONITOR_HEADER_HEIGHT_PX}px); overflow: hidden;"
    ):
        # ── LEFT panel ──
        _build_left_panel(sel, task_manager, _get_task)

            # ── RIGHT panel skeleton ──
        with ui.column().classes(MONITOR_TERMINAL_COL_CLASSES).style("height: 100%;"):
            
            header_row = ui.row().classes(
                f"w-full items-center gap-2 px-2 py-1.5 flex-none {PANEL_HEADER_DARK}"
            )
            
            # Load settings
            SCROLLBACK = int(get_setting("monitor_scrollback", 100000))

            # Layout fix: use standard flex flow (min-w/h=0) instead of absolute pos
            with ui.column().classes(
                "w-full flex-grow overflow-hidden pr-2"
            ).style("min-height: 0; min-width: 0;"):
                
                terminal = ui.xterm({
                    'cursorBlink': True, 
                    'scrollback': SCROLLBACK,
                    'theme': {'background': '#1e1e1e'},
                    'disableStdin': True,      # Crucial: lets browser handle Ctrl+C
                    'rightClickSelectsWord': True, 
                    'cursorInactiveStyle': 'none',
                }).classes(
                    "w-full h-full pl-2 pt-1"
                ).style("min-height: 0; min-width: 0;")
                
                # Dynamically listen to dimension changes to trigger terminal re-wrap
                ui.element('q-resize-observer').on('resize', terminal.fit)
                sel["_terminal"] = terminal

                # 🛠️ JS HACK: Allow Ctrl+C to copy 
                # Embedded directly to avoid browser caching issues with external static scripts
                js_hack = f"""
                setTimeout(() => {{
                    const getEl = window.getElement || ((id) => document.getElementById("c" + id));
                    const widget = getEl({terminal.id});
                    let term = null;
                    if (widget && widget.terminal) term = widget.terminal;
                    else if (widget && widget.$refs && widget.$refs.terminal) term = widget.$refs.terminal;
                    else if (widget && typeof widget.getSelection === 'function') term = widget;

                    if (term) {{
                        term.attachCustomKeyEventHandler((e) => {{
                            if ((e.ctrlKey || e.metaKey) && (e.key === 'c' || e.key === 'C') && term.hasSelection()) {{
                                const text = term.getSelection();
                                if (navigator.clipboard && navigator.clipboard.writeText) {{
                                    navigator.clipboard.writeText(text).catch(() => document.execCommand('copy'));
                                }} else {{
                                    document.execCommand('copy');
                                }}
                                return false; 
                            }}
                            return true;
                        }});
                    }}
                }}, 800);
                """
                ui.run_javascript(js_hack)

    # ── header + placeholder ──
    with header_row:
        header_icon_el = ui.icon("monitor_heart", size="14px").classes("text-slate-500")
        header_label_el = ui.label("Select a task").classes(
            "text-xs font-bold text-white truncate"
        )
        ui.space()
        log_select_el = ui.select(
            [], value=None,
            on_change=lambda e: _on_log_select_change(e.value),
        ).props("outlined dense dark options-dense").classes("w-36")
        log_select_el.set_visibility(False)

    # ----------------------------------------------------------
    #  Core update helpers
    # ----------------------------------------------------------
    async def _rebuild_right():
        task = _get_task(sel["task_id"])
        _update_header(task, header_icon_el, header_label_el, log_select_el, sel)
        sel["_log_offset"] = 0
        
        term = sel.get("_terminal")
        if not term: return

        if not task:
            term.write('\033c')
            term.write("Select a task to view live logs\r\n")
            return

        log_path = _current_log_path()
        if not log_path or not os.path.exists(log_path):
             term.write('\033c')
             term.write(f"Log file not found: {log_path}\r\n")
             return

        # Reset terminal
        term.write('\033c')
        term.write("Loading logs...\r\n")

        import asyncio
        from nicegui import run
        await asyncio.sleep(0.01)
        
        def _read_initial():
            CHUNK_SIZE = int(get_setting("monitor_chunk_size", 50000))
            file_size = os.path.getsize(log_path)
            start_offset = max(0, file_size - (CHUNK_SIZE * 2)) 
            if start_offset > 0:
                with open(log_path, 'rb') as f:
                    f.seek(start_offset)
                    f.readline()  # discard the partial line
                    start_offset = f.tell()
                    
            current_offset = start_offset
            chunks = []
            while current_offset < file_size:
                chunk, new_off = safe_read_log(log_path, current_offset, max_bytes=CHUNK_SIZE)
                if not chunk: break
                chunks.append(chunk)
                current_offset = new_off
            return chunks, current_offset

        try:
            chunks, cur_offset = await run.io_bound(_read_initial)
            term.write('\033c')
            for c in chunks: term.write(c)
            sel["_log_offset"] = cur_offset
        except Exception as e:
            term.write(f"Error reading log: {e}\r\n")

    # ── Live log callback (called by LogEmitter from executor thread) ──
    def _handle_live_log(chunk: str):
        term = sel.get("_terminal")
        if not term:
            return
        term.write(chunk)

    async def _select_task(tid: str):
        # Unsubscribe from the old task first
        old_tid = sel.get("task_id")
        if old_tid:
            log_emitter.unsubscribe(old_tid, _handle_live_log)

        sel["task_id"] = tid
        sel["log_file_name"] = None
        sel["_log_offset"] = 0
        logger.debug("Monitor: selected task %s", tid[:8] if tid else None)

        # Rebuild right panel first so the user sees "Loading..." instantly
        await _rebuild_right()

        # Subscribe for live log push if a task is selected
        if tid:
            log_emitter.subscribe(tid, _handle_live_log)

        # Refresh the pinned task immediately as well
        if sel.get("_pinned_task_view"):
            sel["_pinned_task_view"].refresh()

        # Defer the heavy left-panel DOM diffing (CSS highlight) to avoid
        # blocking the terminal's immediate response
        if sel.get("_task_list_items"):
            ui.timer(0.05, lambda: sel["_task_list_items"].refresh(), once=True)

    def _toggle_export(tid: str, checked: bool):
        if checked:
            sel["export_ids"].add(tid)
        else:
            sel["export_ids"].discard(tid)

    async def _on_log_select_change(name: str):
        sel["log_file_name"] = name
        sel["_log_offset"] = 0
        await _rebuild_right()

    sel["_select_task"] = _select_task
    sel["_toggle_export"] = _toggle_export

    # ----------------------------------------------------------
    #  Reactive updates (status + log file list only, no log polling)
    # ----------------------------------------------------------
    import asyncio
    try:
        main_loop = asyncio.get_running_loop()
    except RuntimeError:
        main_loop = None

    def _refresh_panel():
        pinned = sel.get("_pinned_task_view")
        if pinned:
            pinned.refresh()
        items = sel.get("_task_list_items")
        if items:
            items.refresh()
        pag = sel.get("_task_list_pagination")
        if pag:
            pag.refresh()

    _mon_dirty = {"flag": False}
    # Capture the specific client context 
    client = ui.context.client

    def on_task_manager_change():
        def _do_update():
            if getattr(client, "has_socket_connection", False) is False:
                return
            with client:
                # Throttle UI redraws: just set dirty flag for the 0.5s timer to pick up
                _mon_dirty["flag"] = True
                    
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            if loop and loop.is_running():
                loop.call_soon_threadsafe(_do_update)
            else:
                _do_update()
        except RuntimeError:
            _do_update()

    task_manager.on_change(on_task_manager_change)

    async def _check_mon_dirty():
        if _mon_dirty["flag"]:
            _mon_dirty["flag"] = False
            _refresh_panel()
            
            # also update header icon if current task changed
            fresh = _get_task(sel["task_id"])
            if fresh:
                new_icon = STATUS_ICONS.get(fresh["status"], "help")
                if header_icon_el._props.get("name") != new_icon:
                    header_icon_el._props["name"] = new_icon
                    header_icon_el.classes(
                        replace=STATUS_ICON_COLORS.get(fresh["status"], "text-slate-400")
                    )
                    header_icon_el.update()

        # Refresh log file list (detect new runN.log appearing)
        task = _get_task(sel["task_id"])
        if task:
            opts = get_log_options(task["dir"])
            new_names = list(opts.keys())
            if new_names != list(log_select_el.options or []):
                log_select_el.options = new_names
                log_select_el.set_visibility(len(new_names) > 1)

                # Auto-switch to latest log on file list change
                best_path = resolve_log_path(task["dir"], None)
                best_name = next((n for n, p in opts.items() if p == best_path), None)

                if best_name and best_name != sel.get("log_file_name"):
                    log_select_el.value = best_name
                    await _on_log_select_change(best_name)

                log_select_el.update()

    # 1s timer for task list / status / log file list UI refresh (no log data polling)
    _mon_timer = ui.timer(1.0, _check_mon_dirty)

    async def _on_tab_switch(tab: str):
        if tab == "monitor":
            _refresh_panel()

    from pyruns.utils.events import event_sys

    # Listen for run_root changes from other pages
    def _on_root_changed(new_path):
        _refresh_panel()

    event_sys.on("on_run_root_change", _on_root_changed)
    event_sys.on("on_tab_change", _on_tab_switch)

    def _cleanup_on_disconnect(*_):
        event_sys.off("on_tab_change", _on_tab_switch)
        event_sys.off("on_run_root_change", _on_root_changed)
        _mon_timer.cancel()
        # Unsubscribe from live log push to prevent memory leaks
        current_tid = sel.get("task_id")
        if current_tid:
            log_emitter.unsubscribe(current_tid, _handle_live_log)

    client.on_disconnect(_cleanup_on_disconnect)

# ═══════════════════════════════════════════════════════════════
#  Left panel builder
# ═══════════════════════════════════════════════════════════════

def _build_left_panel(sel: Dict, task_manager, _get_task) -> None:
    with ui.column().classes(
        "flex-none border-r border-slate-200 bg-white gap-0 overflow-hidden"
    ).style(f"width: {MONITOR_PANEL_WIDTH}; height: 100%;"):
        
        with ui.row().classes(
            f"w-full items-center gap-1 px-1 py-2 flex-none {PANEL_HEADER_INDIGO}"
        ):
            ui.icon("monitor_heart", size="16px", color="white")
            ui.label("Tasks").classes("text-xs font-bold text-white")
            ui.space()

            def _manual_refresh():
                task_manager.scan_disk_async()
                ui.notify("Refreshing task list...")

            ui.button(icon="refresh", on_click=_manual_refresh).props(
                "flat dense round size=xs"
            ).classes("text-white/70 hover:text-white").tooltip("Refresh List")

        search_ref = {"val": ""}
        _page = {"value": 0}
        _PAGE_SIZE = int(get_setting("ui_page_size", 50)) or 40

        def _toggle_select_all():
            from pyruns.utils.sort_utils import filter_tasks
            tasks = filter_tasks(list(task_manager.tasks), search_ref["val"])
            
            if not tasks:
                return

            visible_ids = {t["name"] for t in tasks}
            if visible_ids.issubset(sel["export_ids"]):
                sel["export_ids"] -= visible_ids
                ui.notify(f"Deselected {len(visible_ids)} tasks")
            else:
                sel["export_ids"] |= visible_ids
                ui.notify(f"Selected {len(visible_ids)} tasks")
            
            if sel.get("_task_list_items"):
                sel["_task_list_items"].refresh()

        with ui.row().classes(
            "w-full px-0 py-1 flex-none border-b border-slate-100 items-center gap-1 flex-nowrap overflow-hidden"
        ):
            si = ui.textarea(placeholder="Search config / name...").props(
                "dense outlined bg-white clearable autogrow"
            ).classes("flex-grow text-xs font-mono").style("max-height: 80px; overflow-y: auto;")
            # Using a slight debounce for textarea to avoid lag on multi-line paste
            search_timer = [None]

            def _debounced_search(e=None):
                search_ref["val"] = (si.value or "").strip().lower()
                _page["value"] = 0
                if sel.get("_task_list_items"):
                    sel["_task_list_items"].refresh()
                if sel.get("_task_list_pagination"):
                    sel["_task_list_pagination"].refresh()

            def _on_search(e):
                if search_timer[0]:
                    search_timer[0].cancel()
                search_timer[0] = ui.timer(0.3, _debounced_search, once=True)

            si.on_value_change(_on_search)

            def _force_search_now(e=None):
                if search_timer[0]:
                    search_timer[0].cancel()
                _debounced_search()

            si.on("keyup.enter", _force_search_now)
            si.on("clear", _force_search_now)
            
            ui.checkbox(on_change=_toggle_select_all).props(
                "dense size=sm color=indigo"
            ).tooltip("Select/Deselect All Visible")

        @ui.refreshable
        def pinned_task_view():
            if sel.get("task_id"):
                t = _get_task(sel["task_id"])
                if t:
                    with ui.column().classes("w-full gap-0 p-0 m-0 border-b-2 border-indigo-200 bg-indigo-50/50 flex-none"):
                        _task_list_item(t, sel, is_pinned=True)

        sel["_pinned_task_view"] = pinned_task_view
        pinned_task_view()

        with ui.column().classes("flex-grow w-full overflow-y-auto").style("min-height: 0;"):
            @ui.refreshable
            def task_list_items():
                from pyruns.utils.sort_utils import task_sort_key, filter_tasks
                tasks = filter_tasks(list(task_manager.tasks), search_ref["val"])
                tasks.sort(key=task_sort_key, reverse=True)

                total_matches = len(tasks)
                total_pages = max(1, (total_matches + _PAGE_SIZE - 1) // _PAGE_SIZE)
                page = min(_page["value"], total_pages - 1)
                _page["value"] = page

                start = page * _PAGE_SIZE
                end = min(start + _PAGE_SIZE, total_matches)
                visible_tasks = tasks[start:end]

                if not visible_tasks:
                    with ui.column().classes(MONITOR_EMPTY_COL_CLASSES):
                        ui.icon("search_off", size=MONITOR_EMPTY_ICON_SIZE).classes(MONITOR_EMPTY_ICON_CLASSES)
                        ui.label("No tasks").classes(MONITOR_EMPTY_TEXT_CLASSES)
                    return

                with ui.column().classes("w-full gap-0 p-0 m-0 overflow-hidden shrink-0"): 
                    for t in visible_tasks:
                        _task_list_item(t, sel)

            sel["_task_list_items"] = task_list_items
            task_list_items()

        @ui.refreshable
        def task_list_pagination():
            from pyruns.utils.sort_utils import filter_tasks
            tasks = filter_tasks(list(task_manager.tasks), search_ref["val"])
            total_matches = len(tasks)
            total_pages = max(1, (total_matches + _PAGE_SIZE - 1) // _PAGE_SIZE)
            
            if total_pages > 1:
                from pyruns.ui.widgets import pagination_controls
                pagination_controls(
                    page_state=_page,
                    total_pages=total_pages,
                    on_change=lambda: sel.get("_task_list_items") and sel["_task_list_items"].refresh(),
                    container_classes="px-1 py-1 bg-slate-50 border-t border-slate-200 flex-none",
                    align="between",
                    full_width=True,
                    compact=True,
                )

        sel["_task_list_pagination"] = task_list_pagination
        task_list_pagination()

        with ui.row().classes("w-full p-2 flex-none mt-auto"): 
            from pyruns.ui.theme import BTN_PRIMARY
            ui.button(
                "Export Reports", 
                on_click=lambda: show_export_dialog(task_manager, sel["export_ids"]),
            ).props("unelevated icon=download").classes(
                f"{BTN_PRIMARY} w-full text-sm font-bold tracking-wide py-1 rounded"
            )


def _task_list_item(t: Dict[str, Any], sel: Dict, is_pinned: bool = False) -> None:
    tid = t["name"]
    is_active = tid == sel["task_id"]
    status = t["status"]
    icon_name = STATUS_ICONS.get(status, "help")
    icon_cls = STATUS_ICON_COLORS.get(status, "text-slate-400")
    task_name = t.get("name", "unnamed")

    # Sidebar-like active styling (matches sidebar.py _nav_item)
    if is_pinned:
        bg_cls = "bg-indigo-50 shadow-sm"
        border_style = ""
        name_cls = "text-indigo-800 font-bold"
    elif is_active:
        bg_cls = "bg-indigo-50"
        border_style = "border-left: 4px solid #4f46e5;"
        name_cls = "text-indigo-700 font-bold"
    else:
        bg_cls = "hover:bg-slate-50 transition-colors duration-200"
        border_style = "border-left: 4px solid transparent;"
        name_cls = "text-slate-700 font-semibold"

    with ui.row().classes(
        "w-full max-w-full items-center gap-0.5 flex-nowrap min-w-0 overflow-hidden border-b border-slate-50 pr-2"
    ):

        # Checkbox stays intact, independent of hover effects
        ui.checkbox(
            value=tid in sel["export_ids"],
            on_change=lambda e, _tid=tid: sel.get("_toggle_export", lambda x, y: None)(_tid, e.value),
        ).props("dense size=xs color=indigo").classes("shrink-0 pl-1")

        # Inner clickable area — sidebar-like active style
        with ui.column().classes(
            f"flex-1 w-0 min-w-0 justify-center gap-0 cursor-pointer overflow-hidden "
            f"py-1 pl-1.5 rounded monitor-task-item {bg_cls}"
        ).style(border_style).on("click", lambda _, _tid=tid: sel.get("_select_task", lambda x: None)(_tid)):

            # Line 1: task name + icon
            with ui.row().classes("w-full items-center justify-between gap-1 flex-nowrap"):
                with ui.row().classes("items-center gap-1 flex-1 min-w-0 flex-nowrap"):
                    if is_pinned:
                        ui.icon("push_pin", size="10px").classes("text-indigo-500 shrink-0 transform -rotate-45")
                    ui.label(task_name).classes(
                        f"truncate flex-1 min-w-0 text-[11px] {name_cls} leading-snug task-name"
                    ).tooltip(task_name)
                ui.icon(icon_name, size="10px").classes(f"{icon_cls} shrink-0 pr-1")

            # Line 2: status label
            ui.label(status.upper()).classes(
                "truncate w-full text-[9px] text-slate-400 leading-snug"
            )
            

# ═══════════════════════════════════════════════════════════════
#  Small helpers
# ═══════════════════════════════════════════════════════════════

def _update_header(task, icon_el, label_el, select_el, sel):
    if task:
        st = task["status"]
        icon_el._props["name"] = STATUS_ICONS.get(st, "help")
        icon_el.classes(replace=STATUS_ICON_COLORS.get(st, "text-slate-400"))
        icon_el.update()
        label_el.text = task.get("name", "unnamed")
        label_el.update()
        opts = get_log_options(task["dir"])
        names = list(opts.keys())
        select_el.options = names
        if names:
            cur = sel.get("log_file_name")
            if not cur or cur not in names:
                best_path = resolve_log_path(task["dir"], None)
                cur = next((n for n, p in opts.items() if p == best_path), names[-1])
            sel["log_file_name"] = cur
            select_el.value = cur
            select_el.set_visibility(len(names) > 1)
        else:
            sel["log_file_name"] = None
            select_el.set_visibility(False)
        select_el.update()
    else:
        icon_el._props["name"] = "monitor_heart"
        icon_el.classes(replace="text-slate-500")
        icon_el.update()
        label_el.text = "Select a task"
        label_el.update()
        select_el.set_visibility(False)
        select_el.update()
