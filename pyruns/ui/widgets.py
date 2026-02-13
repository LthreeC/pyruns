"""
Reusable UI widgets for Pyruns.
Small, composable building blocks shared across pages.
"""
import os
import json
from nicegui import ui
from typing import Dict, Any, List, Callable, Optional

from pyruns.ui.theme import (
    INPUT_PROPS, BTN_CLASS,
    STATUS_BADGE_STYLES, STATUS_ICONS, STATUS_ICON_COLORS,
)

def choose_directory(initial_dir: str = "") -> str | None:
    """Open a native folder picker dialog."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(initialdir=initial_dir)
        root.destroy()
        return path if path else None
    except Exception:
        ui.notify("Folder picker unavailable in this environment.", type="warning")
        return None


# ═══════════════════════════════════════════════════════════════
#  Global CSS (injected once)
# ═══════════════════════════════════════════════════════════════

_CSS_INJECTED = False

_GLOBAL_CSS = """
/* ── readonly codemirror ── */
.readonly-cm .cm-focused { outline: none !important; }
.readonly-cm .cm-cursor { display: none !important; }
.readonly-cm .cm-activeLine { background: transparent !important; }
.readonly-cm .cm-content { cursor: default; }
.readonly-cm .cm-gutters { cursor: default; }

/* ── dialog: flex chain  card → tab-panels → panel → content ── */
.task-detail-card .q-tab-panels {
    padding: 0 !important;
    display: flex !important;
    flex-direction: column !important;
    flex: 1 1 0 !important;
    min-height: 0 !important;
    overflow: hidden !important;
    background: #1e1e1e !important;
}
/* Quasar wraps panels in an intermediate <div> — make it flex too */
.task-detail-card .q-tab-panels > div {
    display: flex !important;
    flex-direction: column !important;
    flex: 1 1 0 !important;
    min-height: 0 !important;
    overflow: hidden !important;
}
.task-detail-card .q-tab-panel {
    padding: 0 !important;
    display: flex !important;
    flex-direction: column !important;
    flex: 1 1 0 !important;
    min-height: 0 !important;
    overflow: hidden !important;
}

/* ── codemirror: fill remaining flex space ── */
/* .readonly-cm = class we add; nicegui-codemirror = custom-element tag */
.task-detail-card .readonly-cm,
.task-detail-card nicegui-codemirror {
    flex: 1 1 0 !important;
    min-height: 0 !important;
    overflow: hidden !important;
}
.task-detail-card .cm-editor {
    height: 100% !important;
    border: none !important;
}

/* ── Monitor: terminal-like log viewer ── */
.monitor-log-pre {
    font-family: 'Cascadia Code', 'Fira Code', 'Consolas', 'Monaco', monospace;
    font-size: 12px;
    line-height: 1.6;
    color: #d4d4d4;
    white-space: pre-wrap;
    word-break: break-all;
    padding: 8px 12px;
    margin: 0;
}

/* ── Monitor: task list item ── */
.monitor-task-item {
    cursor: pointer;
    transition: all 0.15s ease;
    border-left: 3px solid transparent;
    flex-wrap: nowrap !important;
    overflow: hidden;
}
.monitor-task-item:hover { background: #f1f5f9; }
.monitor-task-item.active {
    background: #eef2ff;
    border-left-color: #6366f1;
}
/* Force label text in task items to never wrap */
.monitor-task-item .nicegui-label,
.monitor-task-item label,
.monitor-task-item span {
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
}
"""


def _ensure_css():
    global _CSS_INJECTED
    if not _CSS_INJECTED:
        ui.add_css(_GLOBAL_CSS)
        _CSS_INJECTED = True


# ═══════════════════════════════════════════════════════════════
#  Directory Picker  (Input + FolderOpen + Refresh)
# ═══════════════════════════════════════════════════════════════

def dir_picker(
    value: str,
    label: str = "Tasks Root",
    on_change: Optional[Callable[[str], None]] = None,
    input_classes: str = "w-72",
) -> ui.input:
    """
    渲染一个 [输入框 + 📂 按钮] 的目录选择组件。
    返回 input 元素以便外部读取 .value。
    """
    inp = ui.input(value=value, label=label).props(INPUT_PROPS).classes(input_classes)

    def _pick():
        path = choose_directory(inp.value or "")
        if path:
            inp.value = path
            if on_change:
                on_change(path)

    def _apply():
        if on_change:
            on_change(inp.value)

    with inp.add_slot("append"):
        ui.button(icon="folder_open", on_click=_pick).props("flat round dense").classes(
            "text-slate-400 hover:text-indigo-600"
        )

    return inp


# ═══════════════════════════════════════════════════════════════
#  Status Badge  (icon + colored pill)
# ═══════════════════════════════════════════════════════════════

def status_badge(status: str, size: str = "sm") -> None:
    """渲染一个状态徽章（带图标 + 文字的圆角标签）。"""
    badge_cls = STATUS_BADGE_STYLES.get(status, "bg-gray-100 text-gray-600")
    icon_name = STATUS_ICONS.get(status, "help")

    if size == "sm":
        with ui.row().classes(f"items-center gap-1 {badge_cls} px-2 py-0.5 rounded-full"):
            ui.icon(icon_name, size="12px")
            ui.label(status.upper()).classes("text-[10px] font-bold tracking-wider")
    else:
        with ui.row().classes(f"items-center gap-1.5 {badge_cls} px-3 py-1 rounded-full"):
            ui.icon(icon_name, size="16px")
            ui.label(status.upper()).classes("text-xs font-bold tracking-wider")


# ═══════════════════════════════════════════════════════════════
#  Status Dot  (minimal icon-only status indicator)
# ═══════════════════════════════════════════════════════════════

def status_dot(status: str, size: str = "14px") -> None:
    """渲染一个小状态图标点。"""
    icon_name = STATUS_ICONS.get(status, "help")
    icon_cls = STATUS_ICON_COLORS.get(status, "text-slate-400")
    ui.icon(icon_name, size=size).classes(icon_cls)


# ═══════════════════════════════════════════════════════════════
#  Readonly Content Viewer  (for task_info / config / log tabs)
# ═══════════════════════════════════════════════════════════════

_LANG_MAP = {"json": "JSON", "yaml": "YAML", "text": None}


def readonly_code_viewer(content: str, mode: str = "text") -> None:
    """
    只读内容查看器，铺满父容器、无白边。
    mode = "json" → CodeMirror + JSON 高亮 (VSCode Dark)
    mode = "yaml" → CodeMirror + YAML 高亮 (VSCode Dark)
    mode = "text" → CodeMirror 纯文本 (VSCode Light)
    mode = "log"  → CodeMirror 无语法高亮 (VSCode Dark，终端风格)
    """
    _ensure_css()

    lang = _LANG_MAP.get(mode)
    theme = "vscodeDark" if mode in ("json", "yaml", "log") else "vscodeLight"
    return ui.codemirror(
        value=content, language=lang, theme=theme,
        line_wrapping=True,
    ).classes("w-full readonly-cm").style(
        "flex:1 1 0; min-height:0; overflow:hidden;"
    )


# ═══════════════════════════════════════════════════════════════
#  Env Editor  (Key=Value pairs with add/remove/save)
# ═══════════════════════════════════════════════════════════════

def env_var_editor(
    rows: List[Dict[str, str]],
    on_save: Callable[[Dict[str, str]], None],
) -> None:
    """
    PyCharm 风格的环境变量表格编辑器。
    rows: [{"key": "K", "val": "V"}, ...]
    on_save: 保存回调，参数为 {key: val} dict。
    """

    def _do_save():
        on_save({r["key"]: r["val"] for r in rows if r["key"]})

    @ui.refreshable
    def _editor():
        with ui.column().classes("w-full h-full gap-0"):

            # ── Toolbar ──
            with ui.row().classes(
                "w-full items-center justify-between px-5 py-2.5 flex-none "
                "bg-gradient-to-r from-indigo-50 to-slate-50 border-b border-indigo-100"
            ):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("vpn_key", size="18px").classes("text-indigo-500")
                    ui.label(f"Environment Variables · {len(rows)}").classes(
                        "text-sm font-bold text-slate-700 tracking-wide"
                    )
                with ui.row().classes("items-center gap-1.5"):
                    ui.button("Add", icon="add", on_click=_add).props(
                        "flat dense no-caps size=sm"
                    ).classes("text-indigo-600 hover:bg-indigo-100 px-2")
                    ui.button("Save", icon="save", on_click=_do_save).props(
                        "unelevated dense no-caps size=sm"
                    ).classes("bg-indigo-600 text-white px-4 rounded-lg")

            # ── Table Header ──
            with ui.row().classes(
                "w-full items-center bg-slate-100 gap-0 flex-none border-b border-slate-200"
            ).style("min-height: 36px; padding: 0 20px;"):
                ui.label("NAME").classes(
                    "w-[35%] text-[11px] font-bold tracking-widest text-slate-500"
                )
                ui.label("VALUE").classes(
                    "flex-grow text-[11px] font-bold tracking-widest text-slate-500"
                )
                ui.element("div").classes("w-8")

            # ── Table Body ──
            with ui.column().classes("w-full gap-0 flex-grow overflow-auto bg-white"):

                if not rows:
                    with ui.column().classes(
                        "w-full items-center justify-center py-20 gap-4"
                    ):
                        ui.icon("add_circle_outline", size="48px").classes(
                            "text-indigo-200"
                        )
                        ui.label("No environment variables").classes(
                            "text-slate-400 text-base"
                        )
                        ui.label("Click \"Add\" to create a new variable").classes(
                            "text-slate-300 text-xs"
                        )
                        ui.button(
                            "Add Variable", icon="add", on_click=_add,
                        ).props("outline no-caps size=sm").classes(
                            "text-indigo-500 border-indigo-300 mt-2 px-4"
                        )

                for i, row in enumerate(rows):
                    bg = "bg-white" if i % 2 == 0 else "bg-slate-50/50"
                    with ui.row().classes(
                        f"w-full items-center gap-0 {bg} "
                        f"border-b border-slate-100 hover:bg-indigo-50/30 transition-colors"
                    ).style("min-height: 44px; padding: 0 20px;"):
                        ui.input(
                            value=row["key"], placeholder="KEY",
                            on_change=lambda e, idx=i: rows[idx].update({"key": e.value}),
                        ).props("dense borderless").classes(
                            "w-[35%] font-mono text-[13px] text-slate-800"
                        )
                        ui.input(
                            value=row["val"], placeholder="VALUE",
                            on_change=lambda e, idx=i: rows[idx].update({"val": e.value}),
                        ).props("dense borderless").classes(
                            "flex-grow font-mono text-[13px] text-slate-700"
                        )
                        ui.button(
                            icon="close", on_click=lambda idx=i: _remove(idx),
                        ).props("flat round dense size=xs").classes(
                            "w-8 text-slate-300 hover:text-red-500 transition-all"
                        )

    def _remove(idx: int):
        if 0 <= idx < len(rows):
            rows.pop(idx)
        _editor.refresh()

    def _add():
        rows.append({"key": "", "val": ""})
        _editor.refresh()

    _editor()


# ═══════════════════════════════════════════════════════════════
#  Section Header  (icon + title, lightweight)
# ═══════════════════════════════════════════════════════════════

def section_header(title: str, icon: str = "list", extra_classes: str = "") -> None:
    """渲染一个小节标题。"""
    with ui.row().classes(f"items-center gap-2 {extra_classes}"):
        ui.icon(icon, size="16px").classes("text-slate-400")
        ui.label(title).classes("text-xs font-bold text-slate-500 uppercase tracking-wider")
