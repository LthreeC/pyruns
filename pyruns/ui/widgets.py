"""
Reusable UI widgets for Pyruns.

Small, composable building blocks shared across pages.
Larger components (env_editor, param_editor, etc.) live in ``ui/components/``.
"""
import os
from nicegui import ui
from typing import Optional, Callable

from pyruns.ui.theme import (
    INPUT_PROPS,
    STATUS_BADGE_STYLES, STATUS_ICONS, STATUS_ICON_COLORS,
)


# ═══════════════════════════════════════════════════════════════
#  Global CSS (injected once per NiceGUI client)
# ═══════════════════════════════════════════════════════════════

_CSS_CLIENTS: set = set()

_CSS_PATH = os.path.join(os.path.dirname(__file__), "static", "pyruns.css")
with open(_CSS_PATH, "r", encoding="utf-8") as _f:
    _GLOBAL_CSS = _f.read()


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
            f"items-center gap-1 {badge_cls} px-2 py-0.5"
        ):
            ui.icon(icon_name, size="12px")
            ui.label(status.upper()).classes(
                "text-[10px] font-bold tracking-wider"
            )
    else:
        with ui.row().classes(
            f"items-center gap-1.5 {badge_cls} px-3 py-1"
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

def readonly_code_viewer(content: str, mode: str = "text") -> None:
    """Full-height readonly CodeMirror viewer.

    *mode*: ``"json"`` | ``"yaml"`` | ``"text"`` | ``"log"``
    """
    _ensure_css()
    # Map mode to CodeMirror language (None = plain text)
    lang = {"json": "JSON", "yaml": "YAML"}.get(mode)
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
    title: str, icon: str = "folder", extra_classes: str = ""
) -> None:
    """Render a unified bold separator header."""
    with ui.row().classes(f"w-full items-center gap-2 mt-2 mb-1 px-1 {extra_classes}"):
        ui.icon(icon, size="16px").classes("text-indigo-600")
        ui.label(title).classes(
            "text-sm font-black tracking-wide text-slate-700"
        )
        ui.separator().classes("flex-grow")

# ═══════════════════════════════════════════════════════════════
#  Pagination widget
# ═══════════════════════════════════════════════════════════════

def pagination_controls(
    page_state: dict,
    total_pages: int,
    on_change: Callable[[], None],
    container_classes: str = "items-center gap-1",
    align: str = "center", # 'center' or 'between' or 'end'
    full_width: bool = False,
    compact: bool = False,
) -> None:
    """Render a reusable page selector with Jump-to-Page input."""
    if total_pages <= 1:
        return

    cur = page_state.get("value", 0)

    def _prev():
        if page_state["value"] > 0:
            page_state["value"] -= 1
            on_change()

    def _next():
        if page_state["value"] < total_pages - 1:
            page_state["value"] += 1
            on_change()

    def _jump_page(e):
        try:
            target = int(e.value) - 1
            if 0 <= target < total_pages:
                if page_state["value"] != target:
                    page_state["value"] = target
                    on_change()
            else:
                e.sender.value = page_state["value"] + 1
        except ValueError:
            pass

    width_cls = "w-full" if full_width else "w-auto"
    wrap_cls = f"{width_cls} items-center justify-{align} {container_classes} flex-nowrap shrink-0"

    btn_size = "xs" if compact else "sm"
    btn_px = "px-1" if compact else "px-2"
    num_w = "w-12" if compact else "w-14"
    text_sz = "text-[10px]" if compact else "text-[11px]"

    with ui.row().classes(wrap_cls):
        b_prev = ui.button(icon="chevron_left", on_click=_prev).props(
            f"outline dense size={btn_size} color=indigo"
        ).classes(f"shadow-sm {btn_px} py-0 min-w-[20px] shrink-0")
        if cur <= 0:
            b_prev.props(add="disable")

        with ui.row().classes("items-center gap-1 flex-nowrap shrink-0"):
            ui.number(
                value=cur + 1, min=1, max=total_pages, step=1, on_change=_jump_page
            ).props('dense outlined debounce="500"').classes(
                f"{num_w} text-center {text_sz}"
            )
            ui.label(f"/ {total_pages}").classes(
                f"{text_sz} font-mono font-bold text-slate-600 shrink-0"
            )

        b_next = ui.button(icon="chevron_right", on_click=_next).props(
            f"outline dense size={btn_size} color=indigo"
        ).classes(f"shadow-sm {btn_px} py-0 min-w-[20px] shrink-0")
        if cur >= total_pages - 1:
            b_next.props(add="disable")
