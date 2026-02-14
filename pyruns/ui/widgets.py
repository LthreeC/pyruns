"""
Reusable UI widgets for Pyruns.

Small, composable building blocks shared across pages.
Larger components (env_editor, param_editor, etc.) live in ``ui/components/``.
"""
import os
from nicegui import ui
from typing import Dict, Optional, Callable

from pyruns.ui.theme import (
    INPUT_PROPS,
    STATUS_BADGE_STYLES, STATUS_ICONS, STATUS_ICON_COLORS,
)


# ═══════════════════════════════════════════════════════════════
#  Global CSS (injected once per NiceGUI client)
# ═══════════════════════════════════════════════════════════════

_CSS_CLIENTS: set = set()

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
.monitor-task-item .nicegui-label,
.monitor-task-item label,
.monitor-task-item span {
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
}
"""


def _ensure_css() -> None:
    """Inject global CSS once per NiceGUI client."""
    try:
        cid = ui.context.client.id
    except Exception:
        cid = "__default__"
    if cid not in _CSS_CLIENTS:
        _CSS_CLIENTS.add(cid)
        ui.add_css(_GLOBAL_CSS)


# ═══════════════════════════════════════════════════════════════
#  Directory Picker
# ═══════════════════════════════════════════════════════════════

def choose_directory(initial_dir: str = "") -> Optional[str]:
    """Open a native folder picker dialog (tkinter)."""
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
        ui.notify(
            "Folder picker unavailable in this environment.",
            type="warning",
        )
        return None


def dir_picker(
    value: str,
    label: str = "Tasks Root",
    on_change: Optional[Callable[[str], None]] = None,
    input_classes: str = "w-72",
) -> ui.input:
    """Render [input + 📂 button] directory picker. Returns the input element."""
    inp = ui.input(
        value=value, label=label
    ).props(INPUT_PROPS).classes(input_classes)

    def _pick():
        path = choose_directory(inp.value or "")
        if path:
            inp.value = path
            if on_change:
                on_change(path)

    with inp.add_slot("append"):
        ui.button(
            icon="folder_open", on_click=_pick
        ).props("flat round dense").classes(
            "text-slate-400 hover:text-indigo-600"
        )

    return inp


# ═══════════════════════════════════════════════════════════════
#  Status Badge / Dot
# ═══════════════════════════════════════════════════════════════

def status_badge(status: str, size: str = "sm") -> None:
    """Render a coloured status pill with icon + text."""
    badge_cls = STATUS_BADGE_STYLES.get(status, "bg-gray-100 text-gray-600")
    icon_name = STATUS_ICONS.get(status, "help")

    if size == "sm":
        with ui.row().classes(
            f"items-center gap-1 {badge_cls} px-2 py-0.5 rounded-full"
        ):
            ui.icon(icon_name, size="12px")
            ui.label(status.upper()).classes(
                "text-[10px] font-bold tracking-wider"
            )
    else:
        with ui.row().classes(
            f"items-center gap-1.5 {badge_cls} px-3 py-1 rounded-full"
        ):
            ui.icon(icon_name, size="16px")
            ui.label(status.upper()).classes(
                "text-xs font-bold tracking-wider"
            )


def status_dot(status: str, size: str = "14px") -> None:
    """Render a minimal icon-only status indicator."""
    icon_name = STATUS_ICONS.get(status, "help")
    icon_cls = STATUS_ICON_COLORS.get(status, "text-slate-400")
    ui.icon(icon_name, size=size).classes(icon_cls)


# ═══════════════════════════════════════════════════════════════
#  Readonly Content Viewer
# ═══════════════════════════════════════════════════════════════

_LANG_MAP = {"json": "JSON", "yaml": "YAML", "text": None}


def readonly_code_viewer(content: str, mode: str = "text") -> None:
    """Full-height readonly CodeMirror viewer.

    *mode*: ``"json"`` | ``"yaml"`` | ``"text"`` | ``"log"``
    """
    _ensure_css()
    lang = _LANG_MAP.get(mode)
    theme = "vscodeDark" if mode in ("json", "yaml", "log") else "vscodeLight"
    return ui.codemirror(
        value=content, language=lang, theme=theme, line_wrapping=True,
    ).classes("w-full readonly-cm").style(
        "flex:1 1 0; min-height:0; overflow:hidden;"
    )


# ═══════════════════════════════════════════════════════════════
#  Section Header
# ═══════════════════════════════════════════════════════════════

def section_header(
    title: str, icon: str = "list", extra_classes: str = ""
) -> None:
    """Lightweight icon + title section divider."""
    with ui.row().classes(f"items-center gap-2 {extra_classes}"):
        ui.icon(icon, size="16px").classes("text-slate-400")
        ui.label(title).classes(
            "text-xs font-bold text-slate-500 uppercase tracking-wider"
        )
