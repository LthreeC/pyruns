"""
Recursive parameter editor – compact multi-column form for config dicts.
Supports: adjustable columns, expand/collapse all, pin important params to the top.
"""
from nicegui import ui
from typing import Dict, Any, List
from pyruns.utils.config_utils import parse_value, get_nested
from pyruns.ui.theme import (
    PINNED_BLOCK_CLASSES, PINNED_HEADER_CLASSES,
    PINNED_TITLE_CLASSES, PINNED_EMPTY_CLASSES
)

# ── Type → icon / color ──
_TYPE_INFO = {
    "bool":   ("check_circle",  "text-indigo-500",  "bg-indigo-50"),
    "number": ("tag",           "text-blue-500",    "bg-blue-50"),
    "list":   ("data_array",    "text-purple-500",  "bg-purple-50"),
    "string": ("text_fields",   "text-slate-400",   "bg-slate-50"),
}

_TINY = "outlined dense bg-white hide-bottom-space"

_EDITOR_CSS_CLIENTS: set = set()

_EDITOR_CSS = """
.param-input .q-field__control { min-height: 26px !important; height: 26px !important; }
.param-input .q-field__marginal { height: 26px !important; }
.param-input .q-field__native { padding: 2px 8px !important; font-size: 12px; }
.param-input input { font-size: 12px !important; }
"""

def _ensure_editor_css():
    """Inject CSS to shrink Quasar input boxes (once per NiceGUI client)."""
    try:
        cid = ui.context.client.id
    except Exception:
        cid = "__default__"
    if cid not in _EDITOR_CSS_CLIENTS:
        _EDITOR_CSS_CLIENTS.add(cid)
        ui.add_css(_EDITOR_CSS)


def _get_type_key(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "list"
    return "string"


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
    _ensure_editor_css()

    if expansions is None:
        expansions = []
    
    if full_data is None:
        full_data = data
        
    if pinned is None:
        # Load from persisted settings on first call
        from pyruns.utils.settings import get as _get_setting
        saved = _get_setting("pinned_params", [])
        pinned = state.setdefault("pinned_params", list(saved))

    # Compatibility: generator.py passes on_star_toggle
    if 'on_star_toggle' in state:
        on_pin_toggle = state.pop('on_star_toggle')

    with container:
        # Render the Pinned block at the root level only
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
                    
                    with ui.grid(columns=columns).classes("w-full gap-1 pt-0.5"):
                        for pd, k, v, pk in valid_pinned:
                            _param_cell(pd, k, v, pk, pinned, state, on_pin_toggle)
                
                # Subheading for "All Parameters"
                with ui.row().classes("w-full items-center gap-1.5 px-0 mt-0 mb-0 pt-0 pb-0"):
                    ui.icon("list", size="14px", color="slate-400")
                    ui.label("All Parameters").classes("text-[11px] font-bold text-slate-400 tracking-widest")


        # Process current level's dictionaries/values
        simple_keys = {
            k: v for k, v in data.items()
            if not isinstance(v, dict) and not k.startswith("_meta")
        }
        dict_keys = {k: v for k, v in data.items() if isinstance(v, dict)}

        # Render unpinned parameters for this level
        unpinned_items = []
        for key, value in simple_keys.items():
            full_key = f"{key_prefix}{key}"
            if full_key not in pinned:
                unpinned_items.append((key, value, full_key))

        if unpinned_items:
            with ui.grid(columns=columns).classes("w-full gap-1"):
                for key, value, full_key in unpinned_items:
                    _param_cell(data, key, value, full_key, pinned, state, on_pin_toggle)

        # Recursively render sub-dictionaries
        for key, value in dict_keys.items():
            exp = ui.expansion(
                f"{key}", icon="folder_open", value=True,
            ).classes(
                "w-full mt-1 border border-slate-200 "
                "overflow-hidden bg-white"
            ).props(
                "dense header-class='bg-slate-50 text-slate-600 "
                "font-bold tracking-wide text-xs py-0.5'"
            )
            expansions.append(exp)
            with exp:
                with ui.column().classes("w-full px-1 py-1 border-t border-slate-100"):
                    recursive_param_editor(
                        ui.column().classes("w-full gap-0"),
                        value, state, task_manager,
                        columns=columns, depth=depth + 1,
                        expansions=expansions, pinned=pinned,
                        key_prefix=f"{key_prefix}{key}.",
                        on_pin_toggle=on_pin_toggle,
                        is_root=False,
                        full_data=full_data,
                    )


def _param_cell(
    data: dict, key: str, value, full_key: str,
    pinned: List[str], state: Dict[str, Any],
    on_pin_toggle: Any = None,
) -> None:
    """Compact param cell with pin toggle."""
    tk = _get_type_key(value)
    icon_name, icon_color, icon_bg = _TYPE_INFO[tk]
    is_pinned = full_key in pinned

    if is_pinned:
        card_cls = (
            "w-full border-2 border-indigo-300 "
            "px-1.5 py-0.5 transition-all duration-100 gap-0 group"
        )
        card_bg = "background:#f5f3ff"  # extremely light indigo (indigo-50 is similar)
    else:
        card_cls = (
            "w-full border border-slate-150 "
            "px-1.5 py-0.5 hover:border-indigo-200 hover:shadow-sm "
            "transition-all duration-100 gap-0 group"
        )
        card_bg = "background:#fafbfc"

    with ui.column().classes(card_cls).style(card_bg):
        # Key row: type icon + label + pin btn
        with ui.row().classes("w-full items-center gap-1"):
            with ui.element("div").classes(
                f"w-4.5 h-4.5 flex items-center justify-center {icon_bg} flex-none"
            ):
                ui.icon(icon_name, size="11px").classes(icon_color)

            ui.label(key).classes(
                "text-xs font-semibold text-slate-600 font-mono truncate leading-none "
                "flex-1 min-w-0"
            ).tooltip(f"{key} ({tk})")

            def toggle_pin(fk=full_key):
                if fk in pinned:
                    pinned.remove(fk)
                else:
                    pinned.append(fk)
                # Persist to _pyruns_.yaml
                from pyruns.utils.settings import save_setting
                save_setting("pinned_params", pinned)
                if on_pin_toggle:
                    try:
                        on_pin_toggle()
                    except Exception:
                        pass

            pin_cls = (
                "text-indigo-500 opacity-100"
                if is_pinned
                else "text-slate-300 opacity-0 group-hover:opacity-60 hover:text-indigo-400"
            )
            star_tip = "Unpin" if is_pinned else "Pin this parameter"
            
            ui.button(icon="push_pin", on_click=toggle_pin).props(
                "flat dense round size=xs padding=0"
            ).classes(f"{pin_cls} min-w-0 transition-opacity duration-200 ml-auto").style(
                "width:18px; height:18px;"
            ).tooltip(star_tip)

        # Value input — ultra-compact with .param-input CSS class
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
