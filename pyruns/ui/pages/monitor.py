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

from pyruns.utils.log_io import read_log
from pyruns.utils.task_io import get_log_options, resolve_log_path
from pyruns.ui.theme import (
    STATUS_ICONS, STATUS_ICON_COLORS, STATUS_ORDER,
    PANEL_HEADER_INDIGO, PANEL_HEADER_DARK, DARK_BG,
)
from pyruns._config import MONITOR_PANEL_WIDTH
from pyruns.ui.widgets import _ensure_css
from pyruns.ui.components.export_dialog import show_export_dialog
from pyruns.utils.ansi_utils import ansi_to_html, tail_lines

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
        ).props("outlined dense dark options-dense").classes("w-36")
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
                logger.warning("Log file not found for task %s", sel["task_id"][:8] if sel.get("task_id") else "?")
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
        logger.debug("Monitor: selected task %s", tid[:8] if tid else None)
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
    #  Snapshot-based polling  (avoids redundant UI rebuilds)
    # ----------------------------------------------------------
    _snap: Dict[str, Any] = {"data": {}, "n": 0}

    def _refresh_panel():
        panel = sel.get("_task_list_panel")
        if panel:
            panel.refresh()

    # ── Initial data guarantee ──
    # Deferred to a once-timer so that the full NiceGUI element tree
    # is committed before we mutate task_manager.tasks and refresh().
    # This is the key fix for the "must visit Manager first" bug:
    # calling scan_disk() synchronously inside a @ui.refreshable
    # render cycle can leave the nested @ui.refreshable in an
    # inconsistent state.
    #
    # Note: TaskManager.__init__ already ran scan_disk(), so tasks are
    # usually present.  We still need the deferred timer for the edge
    # case where Monitor is the *first* tab rendered (before the
    # element tree is committed) — a direct scan_disk+refresh inside
    # the render cycle would corrupt the refreshable state.
    def _initial_load():
        if not task_manager.tasks:
            task_manager.scan_disk()
        _snap["data"] = _task_snap(task_manager)
        _refresh_panel()

    # Only fire the deferred loader if the task list is actually empty
    # (i.e. Monitor was opened before any tab loaded tasks).  Otherwise
    # the initial task_list_panel() render already shows correct data.
    if not task_manager.tasks:
        ui.timer(0.1, _initial_load, once=True)
    else:
        _snap["data"] = _task_snap(task_manager)

    # ── Periodic poll (interval from workspace settings) ──
    _mon_stale = {"flag": False}

    def _poll():
        # Skip expensive UI updates when the monitor tab is hidden.
        # Data refresh (disk I/O) is still done so we're ready
        # to display immediately when the tab becomes visible.
        is_active = state.get("active_tab") == "monitor"

        _snap["n"] += 1

        # Full rescan every ~30 s to detect newly added / deleted tasks
        if _snap["n"] % 30 == 0:
            task_manager.scan_disk()
        else:
            task_manager.refresh_from_disk()

        # Only rebuild the left panel when the snapshot actually changes
        new = _task_snap(task_manager)
        if new != _snap["data"]:
            _snap["data"] = new
            if is_active:
                _refresh_panel()
            else:
                _mon_stale["flag"] = True

        # Catch up when we just became visible again
        if is_active and _mon_stale["flag"]:
            _mon_stale["flag"] = False
            _refresh_panel()

        if not is_active:
            return  # skip right-panel updates while hidden

        # ── Right panel: update log / header for the selected task ──
        task = _get_task(sel["task_id"])
        if not task:
            return

        # Auto-recover: if log_html_el is None but a log file now exists,
        # rebuild the right panel to create the viewer
        if not log_html_el:
            log_path = _current_log_path()
            if log_path and os.path.exists(log_path):
                _rebuild_right()
            return

        opts = get_log_options(task["dir"])
        new_names = list(opts.keys())
        if new_names != list(log_select_el.options or []):
            log_select_el.options = new_names
            log_select_el.set_visibility(len(new_names) > 1)
            log_select_el.update()
        _push_log()

        # Update status icon in header
        fresh = _get_task(sel["task_id"])
        if fresh:
            new_icon = STATUS_ICONS.get(fresh["status"], "help")
            if header_icon_el._props.get("name") != new_icon:
                header_icon_el._props["name"] = new_icon
                header_icon_el.classes(
                    replace=STATUS_ICON_COLORS.get(fresh["status"], "text-slate-400")
                )
                header_icon_el.update()

    ui.timer(float(_get_setting("monitor_poll_interval", 1)), _poll)


# ═══════════════════════════════════════════════════════════════
#  Left panel builder
# ═══════════════════════════════════════════════════════════════

def _build_left_panel(sel: Dict, task_manager, _get_task) -> None:
    """Task list + search + export button."""
    with ui.column().classes(
        "flex-none border-r border-slate-200 bg-white gap-0 overflow-hidden"
    ).style(f"width: {MONITOR_PANEL_WIDTH}; height: 100%;"):
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

        # search + select all
        search_ref = {"val": ""}

        def _toggle_select_all():
            # 1. Start with all tasks
            tasks = list(task_manager.tasks)
            # 2. Apply current filter
            q = search_ref["val"]
            if q:
                tasks = [t for t in tasks if q in t.get("name", "").lower()]
            
            if not tasks:
                return

            visible_ids = {t["id"] for t in tasks}
            # 3. Toggle: if all visible are selected -> deselect all visible
            #            else -> select all visible
            if visible_ids.issubset(sel["export_ids"]):
                sel["export_ids"] -= visible_ids
                ui.notify(f"Deselected {len(visible_ids)} tasks")
            else:
                sel["export_ids"] |= visible_ids
                ui.notify(f"Selected {len(visible_ids)} tasks")
            
            # 4. Refresh list to update checkboxes
            if sel.get("_task_list_panel"):
                sel["_task_list_panel"].refresh()

        with ui.row().classes(
            "w-full px-2 py-1 flex-none border-b border-slate-100 items-center gap-1 no-wrap"
        ):
            si = ui.input(placeholder="Search...").props(
                "dense outlined bg-white clearable"
            ).classes("flex-grow text-xs") # flex-grow to share space
            si.on("keyup.enter", lambda _: sel.get("_task_list_panel") and sel["_task_list_panel"].refresh())
            si.on("clear", lambda _: sel.get("_task_list_panel") and sel["_task_list_panel"].refresh())
            si.on_value_change(
                lambda e: search_ref.update({"val": (e.value or "").strip().lower()})
            )
            
            ui.checkbox(on_change=_toggle_select_all).props(
                "dense size=sm color=indigo"
            ).tooltip("Select/Deselect All Visible")


        # scrollable task list
        @ui.refreshable
        def task_list_panel():
            from pyruns.utils.sort_utils import task_sort_key
            tasks = list(task_manager.tasks)
            q = search_ref["val"]
            if q:
                tasks = [t for t in tasks if q in t.get("name", "").lower()]
            tasks.sort(key=task_sort_key, reverse=True)

            with ui.scroll_area().classes("flex-grow"):
                if not tasks:
                    with ui.column().classes("w-full items-center py-8 gap-2"):
                        ui.icon("search_off", size="32px").classes("text-slate-200")
                        ui.label("No tasks").classes("text-[10px] text-slate-400")
                    return

                with ui.column().classes("w-full gap-0"): 
                    for t in tasks:
                        _task_list_item(t, sel)

        sel["_task_list_panel"] = task_list_panel
        task_list_panel()

        # export button
        with ui.row().classes("w-full p-2 flex-none mt-auto"): 
            ui.button(
                "Export Reports", 
                on_click=lambda: show_export_dialog(task_manager, sel["export_ids"]),
            ).props("unelevated icon=download").classes(
                "w-full bg-indigo-600 text-white text-sm font-bold tracking-wide "
                "py-1 "  # 这里的 py-2 控制高度，flex-grow 必须去掉
                "hover:bg-indigo-700 shadow-md hover:shadow-lg rounded"
            )



def _task_list_item(t: Dict[str, Any], sel: Dict) -> None:
    """Single task row in the left panel."""
    tid = t["id"]
    is_active = tid == sel["task_id"]
    status = t["status"]
    icon_name = STATUS_ICONS.get(status, "help")
    icon_cls = STATUS_ICON_COLORS.get(status, "text-slate-400")
    active_cls = "active" if is_active else ""

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

            # Use cached monitor_count (set by TaskManager) — no per-task I/O
            mon_count = t.get("monitor_count", 0)
            if mon_count:
                ui.badge(str(mon_count)).props(
                    "color=indigo-2 text-color=indigo-8"
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
