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

# 请确保这些导入路径在你项目中是正确的，如果报错请调整为你实际的路径
from pyruns.core.log_io import read_log
from pyruns.core.report import load_monitor_data, get_log_options
from pyruns.ui.theme import (
    STATUS_ICONS, STATUS_ICON_COLORS, STATUS_ORDER,
    PANEL_HEADER_INDIGO, PANEL_HEADER_DARK, DARK_BG,
)
from pyruns.ui.widgets import _ensure_css
from pyruns.ui.components.export_dialog import show_export_dialog
from pyruns.utils.ansi_utils import ansi_to_html, tail_lines

# Header height in px (py-2 ≈ 16px padding + ~36px content)
_HEADER_H = 52


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
        "_log_len": 0,
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
        opts = get_log_options(task["dir"])
        name = sel.get("log_file_name") or (list(opts.keys())[0] if opts else None)
        return opts.get(name) if name else None

    # ── Load data FIRST (before building UI) ──
    if not task_manager.tasks:
        task_manager.scan_disk()
    else:
        task_manager.refresh_from_disk()

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
            "flex-grow min-w-0 gap-0 overflow-hidden"
        ).style("height: 100%;"):
            header_row = ui.row().classes(
                f"w-full items-center gap-2 px-3 py-1.5 flex-none {PANEL_HEADER_DARK}"
            )
            log_container = ui.column().classes(
                "w-full flex-grow overflow-auto"
            ).style(f"min-height: 0; background: {DARK_BG};")

    # ── header + placeholder ──
    log_html_el = None
    with header_row:
        header_icon_el = ui.icon("monitor_heart", size="14px").classes("text-slate-500")
        header_label_el = ui.label("Select a task").classes(
            "text-xs font-bold text-white truncate"
        )
        ui.space()
        log_select_el = ui.select(
            [], value=None,
            on_change=lambda e: _on_log_select_change(e.value),
        ).props("outlined dense dark rounded options-dense").classes("w-36")
        log_select_el.set_visibility(False)

    with log_container:
        _placeholder()

    # ----------------------------------------------------------
    #  Core update helpers
    # ----------------------------------------------------------
    def _rebuild_right():
        nonlocal log_html_el
        task = _get_task(sel["task_id"])
        _update_header(task, header_icon_el, header_label_el, log_select_el, sel)
        sel["_log_len"] = 0
        log_container.clear()

        with log_container:
            if not task:
                _placeholder()
                return
            log_path = _current_log_path()
            if not log_path or not os.path.exists(log_path):
                _no_log_placeholder()
                return
            raw = read_log(log_path)
            text = tail_lines(raw, 3000)
            sel["_log_len"] = len(raw)
            log_html_el = ui.html(
                f'<pre class="monitor-log-pre">{ansi_to_html(text)}</pre>',
                sanitize=False,
            ).classes("w-full").style("min-height: 100%;")
        _scroll_bottom(log_container)

    def _push_log():
        nonlocal log_html_el
        if not log_html_el:
            return
        log_path = _current_log_path()
        if not log_path or not os.path.exists(log_path):
            return
        raw = read_log(log_path)
        prev = sel.get("_log_len", 0)
        if len(raw) == prev:
            return
        if len(raw) < prev:
            sel["_log_len"] = 0
            _rebuild_right()
            return
        new_html = ansi_to_html(raw[prev:])
        sel["_log_len"] = len(raw)
        safe = new_html.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
        ui.run_javascript(f'''
            (() => {{
                const el = document.getElementById("c{log_html_el.id}");
                if (!el) return;
                const pre = el.querySelector("pre");
                if (pre) pre.innerHTML += `{safe}`;
            }})();
        ''')
        _scroll_bottom(log_container)

    # ----------------------------------------------------------
    #  Event handlers
    # ----------------------------------------------------------
    def _select_task(tid: str):
        sel["task_id"] = tid
        sel["log_file_name"] = None
        sel["_log_len"] = 0
        # refresh left panel highlight
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
        sel["_log_len"] = 0
        _rebuild_right()

    # Wire callbacks into sel so left panel can use them
    sel["_select_task"] = _select_task
    sel["_toggle_export"] = _toggle_export

    # ----------------------------------------------------------
    #  1-second poll
    # ----------------------------------------------------------
    def _poll():
        task = _get_task(sel["task_id"])
        if not task:
            return
        task_manager.refresh_from_disk()
        opts = get_log_options(task["dir"])
        new_names = list(opts.keys())
        if new_names != list(log_select_el.options or []):
            log_select_el.options = new_names
            log_select_el.set_visibility(len(new_names) > 1)
            log_select_el.update()
        _push_log()
        # update status icon
        fresh = _get_task(sel["task_id"])
        if fresh:
            new_icon = STATUS_ICONS.get(fresh["status"], "help")
            if header_icon_el._props.get("name") != new_icon:
                header_icon_el._props["name"] = new_icon
                header_icon_el.classes(
                    replace=STATUS_ICON_COLORS.get(fresh["status"], "text-slate-400")
                )
                header_icon_el.update()
        task_list_panel = sel.get("_task_list_panel")
        if task_list_panel:
            task_list_panel.refresh()

    ui.timer(1.0, _poll)

    # Refresh task list panel after data loading (in case new tasks appeared)
    task_list_panel = sel.get("_task_list_panel")
    if task_list_panel:
        task_list_panel.refresh()


# ═══════════════════════════════════════════════════════════════
#  Left panel builder
# ═══════════════════════════════════════════════════════════════

def _build_left_panel(sel: Dict, task_manager, _get_task) -> None:
    """Task list + search + export button."""
    with ui.column().classes(
        "flex-none border-r border-slate-200 bg-white gap-0 overflow-hidden"
    ).style("width: 210px; height: 100%;"):
        # header
        with ui.row().classes(
            f"w-full items-center gap-1.5 px-2.5 py-2 flex-none {PANEL_HEADER_INDIGO}"
        ):
            ui.icon("monitor_heart", size="16px", color="white")
            ui.label("Tasks").classes("text-xs font-bold text-white")
            ui.space()

            def _manual_refresh():
                task_manager.refresh_from_disk()
                tl = sel.get("_task_list_panel")
                if tl:
                    tl.refresh()

            ui.button(icon="refresh", on_click=_manual_refresh).props(
                "flat dense round size=xs"
            ).classes("text-white/70 hover:text-white")

        # search
        search_ref = {"val": ""}
        with ui.row().classes(
            "w-full px-2 py-1 flex-none border-b border-slate-100"
        ):
            si = ui.input(placeholder="Search...").props(
                "dense outlined rounded bg-white clearable"
            ).classes("w-full text-xs")
            si.on("keyup.enter", lambda _: sel.get("_task_list_panel") and sel["_task_list_panel"].refresh())
            si.on("clear", lambda _: sel.get("_task_list_panel") and sel["_task_list_panel"].refresh())
            si.on_value_change(
                lambda e: search_ref.update({"val": (e.value or "").strip().lower()})
            )

        # scrollable task list
        @ui.refreshable
        def task_list_panel():
            tasks = list(task_manager.tasks)
            q = search_ref["val"]
            if q:
                tasks = [t for t in tasks if q in t.get("name", "").lower()]
            tasks.sort(key=lambda t: STATUS_ORDER.get(t["status"], 9))

            with ui.scroll_area().classes("flex-grow"):
                if not tasks:
                    with ui.column().classes("w-full items-center py-8 gap-2"):
                        ui.icon("search_off", size="32px").classes("text-slate-200")
                        ui.label("No tasks").classes("text-[10px] text-slate-400")
                    return

                for t in tasks:
                    _task_list_item(t, sel)

        sel["_task_list_panel"] = task_list_panel
        task_list_panel()

        # export button
        with ui.row().classes(
            "w-full items-center gap-2 px-2 py-2 flex-none "
            "border-t border-slate-200 bg-slate-50"
        ):
            # 【修改 1】：让按钮变高
            # - 删除了 'dense' 和 'size=sm' (props)
            # - 添加了 'h-10' (classes) 来强制设定高度为 40px (之前约为 28px)
            ui.button(
                "Export Reports", icon="download",
                on_click=lambda: show_export_dialog(task_manager, sel["export_ids"]),
            ).props("flat no-caps").classes(
                "text-indigo-600 hover:bg-indigo-50 flex-grow h-15"
            )


def _task_list_item(t: Dict[str, Any], sel: Dict) -> None:
    """Single task row in the left panel."""
    tid = t["id"]
    is_active = tid == sel["task_id"]
    status = t["status"]
    icon_name = STATUS_ICONS.get(status, "help")
    icon_cls = STATUS_ICON_COLORS.get(status, "text-slate-400")
    active_cls = "active" if is_active else ""

    # 获取任务名
    task_name = t.get("name", "unnamed")

    with ui.row().classes(
        f"w-full items-center gap-1.5 px-2 py-1.5 "
        f"monitor-task-item {active_cls} border-b border-slate-50"
    ).style("flex-wrap: nowrap;"):
        # Checkbox: independent click target for export toggle
        ui.checkbox(
            value=tid in sel["export_ids"],
            on_change=lambda e, _tid=tid: sel.get("_toggle_export", lambda x, y: None)(_tid, e.value),
        ).props("dense size=xs color=indigo").classes("flex-none")

        # Clickable area for task selection (icon + name + status)
        with ui.row().classes(
            "items-center gap-1.5 cursor-pointer"
        ).style(
            "flex: 1 1 0; min-width: 0; flex-wrap: nowrap; overflow: hidden;"
        ).on("click", lambda _, _tid=tid: sel.get("_select_task", lambda x: None)(_tid)):
            ui.icon(icon_name, size="14px").classes(f"{icon_cls} flex-none")

            with ui.element("div").style(
                "flex: 1 1 0; min-width: 0; overflow: hidden; "
                "display: flex; flex-direction: column; gap: 0;"
            ):
                ui.label(task_name).style(
                    "white-space: nowrap; overflow: hidden; text-overflow: ellipsis; "
                    "font-size: 11px; font-weight: 600; color: #334155; line-height: 1.25; "
                    "display: block; width: 100%;"
                ).tooltip(task_name)

                ui.label(status.upper()).style(
                    "white-space: nowrap; overflow: hidden; text-overflow: ellipsis; "
                    "font-size: 9px; color: #94a3b8; line-height: 1.25;"
                )

            mon = load_monitor_data(t["dir"])
            if mon:
                ui.badge(str(len(mon))).props(
                    "color=indigo-2 text-color=indigo-8 rounded"
                ).classes("flex-none text-[9px]")


# ═══════════════════════════════════════════════════════════════
#  Small helpers
# ═══════════════════════════════════════════════════════════════

def _update_header(task, icon_el, label_el, select_el, sel):
    """Sync header bar elements with the current task."""
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
            cur = sel.get("log_file_name") or names[0]
            if cur not in names:
                cur = names[0]
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


def _placeholder():
    with ui.column().classes("w-full items-center justify-center gap-3").style(
        f"height: 100%; background: {DARK_BG};"
    ):
        ui.icon("monitor_heart", size="56px").classes("text-slate-700")
        ui.label("Select a task to view live logs").classes(
            "text-sm text-slate-500 font-medium"
        )
        ui.label("Running tasks are shown at the top").classes(
            "text-xs text-slate-600"
        )


def _no_log_placeholder():
    with ui.column().classes("w-full items-center justify-center gap-2").style(
        f"height: 100%; background: {DARK_BG};"
    ):
        ui.icon("article", size="48px").classes("text-slate-600")
        ui.label("No log file available").classes("text-sm text-slate-500")


def _scroll_bottom(container):
    ui.run_javascript(f'''
        (() => {{
            const el = document.getElementById("c{container.id}");
            if (el) el.scrollTop = el.scrollHeight;
        }})();
    ''')