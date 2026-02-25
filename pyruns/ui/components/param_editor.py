"""
Recursive parameter editor – compact multi-column form for config dicts.

Supports: adjustable columns, expand/collapse all, pin important params
to the top with full dotted-path labels (e.g. ``project.name``).
"""
from nicegui import ui
from typing import Dict, Any, List
from pyruns.utils.config_utils import parse_value, get_nested
from pyruns.ui.theme import (
    PINNED_BLOCK_CLASSES, PINNED_HEADER_CLASSES,
    PINNED_TITLE_CLASSES, PINNED_EMPTY_CLASSES
)

# ── Type → (icon, text_color, bg_color) ──
_TYPE_INFO = {
    "bool":   ("check_circle", "text-indigo-500", "bg-indigo-50"),
    "number": ("tag",          "text-blue-500",   "bg-blue-50"),
    "list":   ("data_array",   "text-purple-500", "bg-purple-50"),
    "string": ("text_fields",  "text-slate-400",  "bg-slate-50"),
}

_TINY = "outlined dense bg-white hide-bottom-space"


def _get_type_key(value) -> str:
    """Map a Python value to its type category string."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "list"
    return "string"


# ═══════════════════════════════════════════════════════════════
#  Main recursive editor
# ═══════════════════════════════════════════════════════════════

def recursive_param_editor(
    container,
    data: Dict[str, Any],
    state: Dict[str, Any],
    task_manager,
    columns: int = 2,
    depth: int = 0,
    expansions: List = None,
    pinned: List[str] = None,
    key_prefix: str = "",
    on_pin_toggle: Any = None,
    is_root: bool = True,
    full_data: Dict[str, Any] = None,
) -> None:
    """Build a compact form editor for a (possibly nested) config dict."""

    if expansions is None:
        expansions = []
    if full_data is None:
        full_data = data
    if pinned is None:
        from pyruns.utils.settings import get as _get_setting
        saved = _get_setting("pinned_params", [])
        pinned = state.setdefault("pinned_params", list(saved))

    # Compat: generator.py may pass on_star_toggle (legacy name)
    if 'on_star_toggle' in state:
        on_pin_toggle = state.pop('on_star_toggle')

    with container:
        # ── Pinned section (root level only) ──
        if is_root:
            valid_pinned = []
            for pk in pinned:
                pd, k, v = get_nested(full_data, pk)
                if pd is not None:
                    valid_pinned.append((pd, k, v, pk))

            if valid_pinned:
                with ui.column().classes(PINNED_BLOCK_CLASSES):
                    with ui.row().classes(PINNED_HEADER_CLASSES):
                        ui.icon("push_pin", size="14px", color="indigo-500")
                        ui.label("Pinned Parameters").classes(PINNED_TITLE_CLASSES)

                    with ui.grid(columns=columns).classes("w-full gap-x-0.5 gap-y-0 p-0 m-0"):
                        for pd, k, v, pk in valid_pinned:
                            # Show full dotted path as display label
                            _param_cell(pd, k, v, pk, pinned, state, on_pin_toggle,
                                        display_key=pk)

                # "All Parameters" subheading
                with ui.row().classes("w-full items-center gap-1.5 px-0 m-0 p-0").style("min-height:20px"):
                    ui.icon("list", size="14px", color="slate-400")
                    ui.label("All Parameters").classes(
                        "text-[11px] font-bold text-slate-400 tracking-widest leading-none p-0 m-0"
                    )

        # ── Separate simple keys vs dict keys ──
        simple_keys = {
            k: v for k, v in data.items()
            if not isinstance(v, dict) and not k.startswith("_meta")
        }
        dict_keys = {k: v for k, v in data.items() if isinstance(v, dict)}

        # ── Unpinned leaf parameters at this level ──
        unpinned = [
            (k, v, f"{key_prefix}{k}")
            for k, v in simple_keys.items()
            if f"{key_prefix}{k}" not in pinned
        ]
        if unpinned:
            with ui.grid(columns=columns).classes("w-full gap-x-0.5 gap-y-0"):
                for key, value, full_key in unpinned:
                    _param_cell(data, key, value, full_key, pinned, state, on_pin_toggle)

        # ── Nested dicts → expansion items ──
        for key, value in dict_keys.items():
            exp = ui.expansion(
                f"{key}", icon="folder_open", value=True,
            ).classes(
                "w-full m-0 p-0 border border-slate-200 "
                "overflow-hidden bg-white param-expansion"
            ).props(
                "dense header-class='bg-slate-50 text-slate-600 font-bold tracking-wide text-xs' "
                "content-class='p-0 m-0' "
                "content-style='padding:0!important;margin:0!important;'"
            )
            expansions.append(exp)
            with exp:
                recursive_param_editor(
                    ui.column().classes(
                        "w-full pl-3 py-0 m-0 gap-0 border-t border-slate-100"
                    ).style("padding-top:0;padding-bottom:0"),
                    value, state, task_manager,
                    columns=columns, depth=depth + 1,
                    expansions=expansions, pinned=pinned,
                    key_prefix=f"{key_prefix}{key}.",
                    on_pin_toggle=on_pin_toggle,
                    is_root=False,
                    full_data=full_data,
                )


# ═══════════════════════════════════════════════════════════════
#  Single parameter cell
# ═══════════════════════════════════════════════════════════════

def _param_cell(
    data: dict, key: str, value, full_key: str,
    pinned: List[str], state: Dict[str, Any],
    on_pin_toggle: Any = None,
    display_key: str = None,
) -> None:
    """Render one parameter card: icon + label + value input + pin button.

    Parameters
    ----------
    display_key : str, optional
        Label to show. Defaults to ``key`` (leaf name) but pinned params
        pass the full dotted path (e.g. ``project.name``) for clarity.
    """
    label = display_key or key
    tk = _get_type_key(value)
    icon_name, icon_color, icon_bg = _TYPE_INFO[tk]
    is_pinned = full_key in pinned

    # Card style: pinned gets a highlight border
    card_cls = (
        "w-full px-0.5 py-0 transition-all duration-100 gap-0 group "
        + ("border-2 border-indigo-300" if is_pinned
           else "border border-slate-150 hover:border-indigo-200 hover:shadow-sm")
    )
    card_bg = "background:#f5f3ff" if is_pinned else "background:#fafbfc"

    with ui.column().classes(card_cls).style(card_bg):
        # ── Key row: type icon + label + pin button ──
        with ui.row().classes("w-full items-center gap-1"):
            with ui.element("div").classes(
                f"w-4.5 h-4.5 flex items-center justify-center {icon_bg} flex-none"
            ):
                ui.icon(icon_name, size="11px").classes(icon_color)

            ui.label(label).classes(
                "text-xs font-semibold text-slate-600 font-mono truncate leading-none "
                "flex-1 min-w-0"
            ).tooltip(f"{full_key} ({tk})")

            def toggle_pin(fk=full_key):
                if fk in pinned:
                    pinned.remove(fk)
                else:
                    pinned.append(fk)
                from pyruns.utils.settings import save_setting
                save_setting("pinned_params", pinned)
                if on_pin_toggle:
                    try:
                        on_pin_toggle()
                    except Exception:
                        pass

            pin_cls = (
                "text-indigo-500 opacity-100" if is_pinned
                else "text-slate-300 opacity-0 group-hover:opacity-60 hover:text-indigo-400"
            )
            ui.button(icon="push_pin", on_click=toggle_pin).props(
                "flat dense round size=xs padding=0"
            ).classes(
                f"{pin_cls} min-w-0 transition-opacity duration-200 ml-auto"
            ).style("width:18px;height:18px;").tooltip(
                "Unpin" if is_pinned else "Pin this parameter"
            )

        # ── Value input ──
        def on_change(e, d=data, k=key):
            d[k] = parse_value(e.value)

        if isinstance(value, bool):
            ui.switch(value=value, on_change=on_change).props("dense color=indigo")
        elif isinstance(value, list):
            ui.input(value=str(value), on_change=on_change).props(_TINY).classes(
                "w-full font-mono text-xs text-purple-700 param-input"
            ).tooltip("[1, 2, 3]")
        elif isinstance(value, (int, float)):
            ui.input(value=str(value), on_change=on_change).props(_TINY).classes(
                "w-full font-mono text-xs text-blue-700 param-input"
            )
        else:
            ui.input(value=str(value), on_change=on_change).props(_TINY).classes(
                "w-full font-mono text-xs text-slate-700 param-input"
            )
