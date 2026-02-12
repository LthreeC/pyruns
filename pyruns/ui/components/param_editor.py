"""
Recursive parameter editor – ultra-compact multi-column form for config dicts.
Supports: adjustable columns, expand/collapse all, star (pin) important params.
"""
from nicegui import ui
from typing import Dict, Any, List, Set
from pyruns.utils.config_utils import parse_value
from pyruns.ui.theme import INPUT_PROPS

# ── Type → icon / color ──
_TYPE_INFO = {
    "bool":   ("toggle_on",    "text-indigo-500",  "bg-indigo-50"),
    "number": ("tag",          "text-blue-500",    "bg-blue-50"),
    "list":   ("data_array",   "text-purple-500",  "bg-purple-50"),
    "string": ("text_fields",  "text-slate-400",   "bg-slate-100"),
}

_TINY = "outlined dense rounded bg-white hide-bottom-space"


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
    starred: Set[str] = None,
    key_prefix: str = "",
    on_star_toggle: Any = None,
) -> None:
    """Build a compact form editor for a (possibly nested) config dict.

    Args:
        expansions: shared list to collect ui.expansion refs for expand/collapse all.
        starred: shared set of starred param full-key paths.
        key_prefix: dot-separated path prefix for unique star keys.
        on_star_toggle: callback function to invoke when a star is toggled (e.g. refresh editor).
    """
    if expansions is None:
        expansions = []
    if starred is None:
        starred = state.setdefault("starred_params", set())

    with container:
        simple_keys = {
            k: v for k, v in data.items()
            if not isinstance(v, dict) and not k.startswith("_meta")
        }
        dict_keys = {k: v for k, v in data.items() if isinstance(v, dict)}

        if simple_keys:
            items = list(simple_keys.items())
            with ui.grid(columns=columns).classes("w-full gap-1"):
                for key, value in items:
                    full_key = f"{key_prefix}{key}" if key_prefix else key
                    _param_cell(data, key, value, full_key, starred, state, on_star_toggle)

        for key, value in dict_keys.items():
            exp = ui.expansion(
                f"{key}", icon="folder_open", value=True,
            ).classes(
                "w-full mt-1 border border-slate-200 rounded-md "
                "overflow-hidden bg-white shadow-sm"
            ).props(
                "dense header-class='bg-slate-50 text-slate-600 "
                "font-bold tracking-wide text-[11px] py-0.5'"
            )
            expansions.append(exp)
            with exp:
                with ui.column().classes("w-full px-1 py-1 border-t border-slate-100"):
                    recursive_param_editor(
                        ui.column().classes("w-full gap-0"),
                        value, state, task_manager,
                        columns=columns, depth=depth + 1,
                        expansions=expansions, starred=starred,
                        key_prefix=f"{key_prefix}{key}.",
                        on_star_toggle=on_star_toggle,
                    )


def _param_cell(
    data: dict, key: str, value, full_key: str,
    starred: Set[str], state: Dict[str, Any],
    on_star_toggle: Any = None,
) -> None:
    """Ultra-compact param cell with star toggle."""
    tk = _get_type_key(value)
    icon_name, icon_color, icon_bg = _TYPE_INFO[tk]
    is_starred = full_key in starred

    # Card styling based on star state
    if is_starred:
        card_cls = (
            "w-full border-2 border-amber-300 rounded-md "
            "px-1.5 py-1 transition-all duration-100 gap-0"
        )
        card_bg = "background:#fffbeb"  # warm amber tint
    else:
        card_cls = (
            "w-full border border-slate-100 rounded-md "
            "px-1.5 py-1 hover:border-indigo-200 "
            "transition-all duration-100 gap-0"
        )
        card_bg = "background:#fafbfc"

    with ui.column().classes(card_cls).style(card_bg) as cell:
        # Key row: star btn + type icon + label
        with ui.row().classes("w-full items-center gap-0.5"):
            # Star toggle button
            def toggle_star(fk=full_key):
                if fk in starred:
                    starred.discard(fk)
                else:
                    starred.add(fk)
                # Use the callback passed from the parent
                if on_star_toggle:
                    try:
                        on_star_toggle()
                    except Exception:
                        pass

            star_icon = "star" if is_starred else "star_border"
            star_color = "text-amber-400" if is_starred else "text-slate-300 hover:text-amber-300"
            star_tip = "取消收藏" if is_starred else "收藏此参数"
            ui.button(icon=star_icon, on_click=toggle_star).props(
                "flat dense round size=xs padding=0"
            ).classes(f"{star_color} min-w-0").style(
                "width:16px; height:16px;"
            ).tooltip(star_tip)

            # Type icon
            with ui.element("div").classes(
                f"w-4 h-4 rounded flex items-center justify-center {icon_bg} flex-none"
            ):
                ui.icon(icon_name, size="10px").classes(icon_color)

            # Key label
            ui.label(key).classes(
                "text-[10px] font-semibold text-slate-500 font-mono truncate leading-none"
            ).tooltip(f"{key} ({tk})")

        # Value input
        def on_change(e, d=data, k=key):
            d[k] = parse_value(e.value)

        if isinstance(value, bool):
            ui.switch(value=value, on_change=on_change).props("dense color=indigo")
        elif isinstance(value, list):
            ui.input(value=str(value), on_change=on_change).props(_TINY).classes(
                "w-full font-mono text-[10px] text-purple-700"
            ).tooltip("[1, 2, 3]")
        elif isinstance(value, (int, float)):
            ui.input(value=str(value), on_change=on_change).props(_TINY).classes(
                "w-full font-mono text-[10px] text-blue-700"
            )
        else:
            ui.input(value=str(value), on_change=on_change).props(_TINY).classes(
                "w-full font-mono text-[10px] text-slate-700"
            )
