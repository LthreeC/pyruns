"""
Monitor Page – real-time ANSI-colored log viewer + export reports.

Design:
  • Left panel : task list with search + export button at bottom
  • Right panel: persistent log viewer updated via JS (no refreshable flicker)
  • 1-second polling pushes only new log bytes
  • Uses explicit calc(100vh - Xpx) height to avoid flex-chain issues
    with NiceGUI / Quasar intermediate wrapper divs.
"""
import os
from typing import Dict, Any, Optional
from nicegui import ui

# Direct safe-read logic moved to utils/log_io.py for robustness
from pyruns.utils.task_io import get_log_options, resolve_log_path
from pyruns.utils.log_io import safe_read_log
from pyruns.utils.settings import get as get_setting
from pyruns.ui.theme import (
    STATUS_ICONS, STATUS_ICON_COLORS,
    PANEL_HEADER_INDIGO, PANEL_HEADER_DARK,
)
from pyruns.ui.theme import MONITOR_PANEL_WIDTH
from pyruns.ui.widgets import _ensure_css
from pyruns.ui.components.export_dialog import show_export_dialog

from pyruns.utils import get_logger

logger = get_logger(__name__)

# Header height in px (py-2 ≈ 16px padding + ~36px content)
_HEADER_H = 52


# ═══════════════════════════════════════════════════════════════
#  Lightweight snapshot — O(n) memory, zero I/O
# ═══════════════════════════════════════════════════════════════

def _task_snap(task_manager) -> Dict[str, tuple]:
    """Return {id: (status, progress, monitor_count)} for quick diff."""
    return {
        t["id"]: (t["status"], t.get("progress", 0), t.get("monitor_count", 0))
        for t in task_manager.tasks
    }



# ═══════════════════════════════════════════════════════════════
#  Entrypoint
# ═══════════════════════════════════════════════════════════════

def render_monitor_page(state: Dict[str, Any], task_manager) -> None:
    from pyruns.utils.settings import get as _get_setting
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
        return next((t for t in task_manager.tasks if t["id"] == tid), None)

    def _current_log_path() -> Optional[str]:
        task = _get_task(sel["task_id"])
        if not task:
            return None
        return resolve_log_path(task["dir"], sel.get("log_file_name"))

    # ═════════════════════════════════════════════════════════
    #  Two-column layout — explicit height via calc()
    # ═════════════════════════════════════════════════════════
    with ui.row().classes("w-full gap-0 flex-nowrap").style(
        f"height: calc(100vh - {_HEADER_H}px); overflow: hidden;"
    ):
        # ── LEFT panel ──
        _build_left_panel(sel, task_manager, _get_task)

            # ── RIGHT panel skeleton ──
        with ui.column().classes(
            "flex-grow min-w-0 gap-0 overflow-hidden bg-[#1e1e1e]"
        ).style("height: 100%;"):
            
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
                
                # 动态监听大小变化，触发终端排版重计算
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
    def _rebuild_right():
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

        # Prevent browser freeze on huge logs: initial load limited to last N bytes
        # (defined by monitor_chunk_size * 2)
        # PERFORMANCE NOTE: uses seek() + read(chunk), so it is O(1) and safe for any file size.
        CHUNK_SIZE = int(get_setting("monitor_chunk_size", 50000))
        
        file_size = os.path.getsize(log_path)
        start_offset = max(0, file_size - (CHUNK_SIZE * 2)) 
        
        # 如果不是从头读，可能会切掉半行，所以必须寻找下一个 \n
        if start_offset > 0:
            with open(log_path, 'rb') as f:
                f.seek(start_offset)
                f.readline()  # 丢弃被切断的残行
                start_offset = f.tell()
                
        current_offset = start_offset
        
        # 安全读取并写入
        while current_offset < file_size:
            chunk, new_off = safe_read_log(log_path, current_offset, max_bytes=CHUNK_SIZE)
            if not chunk:
                break
            term.write(chunk)
            current_offset = new_off
            
        sel["_log_offset"] = current_offset

    def _push_log():
        if not sel["task_id"]:
             return

        term = sel.get("_terminal")
        if not term: return

        log_path = _current_log_path()
        if not log_path or not os.path.exists(log_path):
            return
            
        file_size = os.path.getsize(log_path)
        offset = sel.get("_log_offset", 0)
        
        if file_size < offset:
             # 文件被截断/重写了
             sel["_log_offset"] = 0
             _rebuild_right()
             return

        if file_size == offset:
            return

        # 增量读取新日志 (Incremental Read)
        CHUNK_SIZE = int(get_setting("monitor_chunk_size", 50000))
        new_text, new_offset = safe_read_log(log_path, offset, max_bytes=CHUNK_SIZE)
        
        if new_text:
            term.write(new_text)
            sel["_log_offset"] = new_offset

    def _select_task(tid: str):
        sel["task_id"] = tid
        sel["log_file_name"] = None
        sel["_log_offset"] = 0
        logger.debug("Monitor: selected task %s", tid[:8] if tid else None)
        task_list_panel = sel.get("_task_list_panel")
        if task_list_panel:
            task_list_panel.refresh()
        _rebuild_right()

    def _toggle_export(tid: str, checked: bool):
        if checked:
            sel["export_ids"].add(tid)
        else:
            sel["export_ids"].discard(tid)

    def _on_log_select_change(name: str):
        sel["log_file_name"] = name
        sel["_log_offset"] = 0
        _rebuild_right()

    sel["_select_task"] = _select_task
    sel["_toggle_export"] = _toggle_export

    # ----------------------------------------------------------
    #  Snapshot-based polling 
    # ----------------------------------------------------------
    _snap: Dict[str, Any] = {"data": {}, "n": 0}

    def _refresh_panel():
        panel = sel.get("_task_list_panel")
        if panel:
            panel.refresh()

    def _initial_load():
        if not task_manager.tasks:
            task_manager.scan_disk()
        _snap["data"] = _task_snap(task_manager)
        _refresh_panel()

    if not task_manager.tasks:
        ui.timer(0.1, _initial_load, once=True)
    else:
        _snap["data"] = _task_snap(task_manager)

    _mon_stale = {"flag": False}

    def _poll():
        is_active = state.get("active_tab") == "monitor"
        _snap["n"] += 1

        if _snap["n"] % 30 == 0:
            task_manager.scan_disk()
        else:
            task_manager.refresh_from_disk()

        new = _task_snap(task_manager)
        if new != _snap["data"]:
            _snap["data"] = new
            if is_active:
                _refresh_panel()
            else:
                _mon_stale["flag"] = True

        if is_active and _mon_stale["flag"]:
            _mon_stale["flag"] = False
            _refresh_panel()

        if not is_active:
            return 

        task = _get_task(sel["task_id"])
        if not task:
            return

        opts = get_log_options(task["dir"])
        new_names = list(opts.keys())
        if new_names != list(log_select_el.options or []):
            log_select_el.options = new_names
            log_select_el.set_visibility(len(new_names) > 1)
            
            # Auto-switch to latest log on file list change (e.g. new run started)
            best_path = resolve_log_path(task["dir"], None)
            best_name = next((n for n, p in opts.items() if p == best_path), None)
            
            if best_name and best_name != sel.get("log_file_name"):
                 log_select_el.value = best_name
                 _on_log_select_change(best_name) # Reset offset & rebuild view
            
            log_select_el.update()
            
        _push_log()

        fresh = _get_task(sel["task_id"])
        if fresh:
            new_icon = STATUS_ICONS.get(fresh["status"], "help")
            if header_icon_el._props.get("name") != new_icon:
                header_icon_el._props["name"] = new_icon
                header_icon_el.classes(
                    replace=STATUS_ICON_COLORS.get(fresh["status"], "text-slate-400")
                )
                header_icon_el.update()

    ui.timer(float(get_setting("monitor_poll_interval", 1)), _poll)

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
                task_manager.refresh_from_disk(force_all=True)
                tl = sel.get("_task_list_panel")
                if tl:
                    tl.refresh()
                ui.notify("Refreshed all tasks")

            ui.button(icon="refresh", on_click=_manual_refresh).props(
                "flat dense round size=xs"
            ).classes("text-white/70 hover:text-white").tooltip("Refresh List")

        search_ref = {"val": ""}

        def _toggle_select_all():
            tasks = list(task_manager.tasks)
            q = search_ref["val"]
            if q:
                tasks = [t for t in tasks if q in t.get("name", "").lower()]
            
            if not tasks:
                return

            visible_ids = {t["id"] for t in tasks}
            if visible_ids.issubset(sel["export_ids"]):
                sel["export_ids"] -= visible_ids
                ui.notify(f"Deselected {len(visible_ids)} tasks")
            else:
                sel["export_ids"] |= visible_ids
                ui.notify(f"Selected {len(visible_ids)} tasks")
            
            if sel.get("_task_list_panel"):
                sel["_task_list_panel"].refresh()

        with ui.row().classes(
            "w-full px-0 py-1 flex-none border-b border-slate-100 items-center gap-1 flex-nowrap overflow-hidden"
        ):
            si = ui.input(placeholder="Search...").props(
                "dense outlined bg-white clearable"
            ).classes("flex-grow text-xs")
            si.on("keyup.enter", lambda _: sel.get("_task_list_panel") and sel["_task_list_panel"].refresh())
            si.on("clear", lambda _: sel.get("_task_list_panel") and sel["_task_list_panel"].refresh())
            si.on_value_change(
                lambda e: search_ref.update({"val": (e.value or "").strip().lower()})
            )
            
            ui.checkbox(on_change=_toggle_select_all).props(
                "dense size=sm color=indigo"
            ).tooltip("Select/Deselect All Visible")

        @ui.refreshable
        def task_list_panel():
            from pyruns.utils.sort_utils import task_sort_key
            tasks = list(task_manager.tasks)
            q = search_ref["val"]
            if q:
                tasks = [t for t in tasks if q in t.get("name", "").lower()]
            tasks.sort(key=task_sort_key, reverse=True)

            # 🛠️ 终极修复 1：滚动区域本身必须限宽，防止内部元素把它撑开
            with ui.scroll_area().classes("flex-grow w-full overflow-hidden"):
                if not tasks:
                    with ui.column().classes("w-full items-center py-8 gap-2"):
                        ui.icon("search_off", size="32px").classes("text-slate-200")
                        ui.label("No tasks").classes("text-[10px] text-slate-400")
                    return

                # 🛠️ 终极修复 2：加回 w-full 限制宽度，加上 p-0 m-0 消除 NiceGUI 默认可能带来的边距撑破
                with ui.column().classes("w-full gap-0 p-0 m-0 overflow-hidden shrink-0"): 
                    for t in tasks:
                        _task_list_item(t, sel)

        sel["_task_list_panel"] = task_list_panel
        task_list_panel()

        with ui.row().classes("w-full p-2 flex-none mt-auto"): 
            ui.button(
                "Export Reports", 
                on_click=lambda: show_export_dialog(task_manager, sel["export_ids"]),
            ).props("unelevated icon=download").classes(
                "w-full bg-indigo-600 text-white text-sm font-bold tracking-wide "
                "py-1 hover:bg-indigo-700 shadow-md hover:shadow-lg rounded"
            )


def _task_list_item(t: Dict[str, Any], sel: Dict) -> None:
    tid = t["id"]
    is_active = tid == sel["task_id"]
    status = t["status"]
    icon_name = STATUS_ICONS.get(status, "help")
    icon_cls = STATUS_ICON_COLORS.get(status, "text-slate-400")
    active_cls = "active" if is_active else ""
    task_name = t.get("name", "unnamed")

    # 🛠️ 终极修复 3：加回 w-full 和 max-w-full！这告诉内部文本“宽度就这么多，超出的给我变成省略号”
    with ui.row().classes(
        "w-full max-w-full items-center gap-0.5 flex-nowrap min-w-0 overflow-hidden border-b border-slate-50"
    ):
        
        # Checkbox 保持不变，独立于 Hover 效果之外
        ui.checkbox(
            value=tid in sel["export_ids"],
            on_change=lambda e, _tid=tid: sel.get("_toggle_export", lambda x, y: None)(_tid, e.value),
        ).props("dense size=xs color=indigo").classes("shrink-0")

        # 内部可点击区域（Hover 背景变化只在这里生效）
        with ui.row().classes(
            f"flex-1 w-0 min-w-0 items-center gap-1 cursor-pointer flex-nowrap overflow-hidden "
            f"py-0.5 rounded monitor-task-item {active_cls} hover:bg-slate-100 transition-colors"
        ).on("click", lambda _, _tid=tid: sel.get("_select_task", lambda x: None)(_tid)):

            # 文本容器
            with ui.element("div").classes("flex flex-col flex-1 w-0 min-w-0 gap-0 overflow-hidden"):
                ui.label(task_name).classes(
                    "truncate w-full text-[11px] font-semibold text-slate-700 leading-snug"
                ).tooltip(task_name)

                ui.label(status.upper()).classes(
                    "truncate w-full text-[9px] text-slate-400 leading-snug"
                )
                
            ui.icon(icon_name, size="9px").classes(f"{icon_cls} shrink-0")
            

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
            cur = sel.get("log_file_name") or names[-1]
            if cur not in names:
                cur = names[-1]
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
