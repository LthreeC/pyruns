"""Recursive parameter editor for the Generator page."""

from typing import Any, Dict, List

from nicegui import ui

from pyruns.ui.theme import (
    PINNED_BLOCK_CLASSES,
    PINNED_HEADER_CLASSES,
    PINNED_TITLE_CLASSES,
)
from pyruns.utils.config_utils import get_nested, parse_value

_TYPE_INFO = {
    "bool": ("check_circle", "text-indigo-500", "bg-indigo-50"),
    "number": ("tag", "text-blue-500", "bg-blue-50"),
    "list": ("data_array", "text-cyan-600", "bg-cyan-50"),
    "string": ("text_fields", "text-slate-400", "bg-slate-50"),
}

_TINY = "outlined dense bg-white hide-bottom-space"
_PARAM_GRID_MIN_COL_PX = 240


def _get_type_key(value: Any) -> str:
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
    expansions: List | None = None,
    pinned: List[str] | None = None,
    key_prefix: str = "",
    on_pin_toggle: Any = None,
    is_root: bool = True,
    full_data: Dict[str, Any] | None = None,
) -> None:
    """Build a compact editor for a nested config dict."""

    if expansions is None:
        expansions = []
    if full_data is None:
        full_data = data
    if pinned is None:
        from pyruns.utils.settings import get as _get_setting

        saved = _get_setting("pinned_params", [])
        pinned = state.setdefault("pinned_params", list(saved))

    if "on_star_toggle" in state:
        on_pin_toggle = state.pop("on_star_toggle")

    with container:
        if is_root:
            _render_pinned_block(full_data, pinned, state, columns, on_pin_toggle)

        simple_keys = {
            key: value
            for key, value in data.items()
            if not isinstance(value, dict) and not key.startswith("_meta")
        }
        dict_keys = {key: value for key, value in data.items() if isinstance(value, dict)}

        unpinned = [
            (key, value, f"{key_prefix}{key}")
            for key, value in simple_keys.items()
            if f"{key_prefix}{key}" not in pinned
        ]
        if unpinned:
            with _param_grid(columns):
                for key, value, full_key in unpinned:
                    _param_cell(data, key, value, full_key, pinned, on_pin_toggle)

        for key, value in dict_keys.items():
            expansion = ui.expansion(
                key,
                icon="folder_open",
                value=(depth == 0),
            ).classes(
                "w-full m-0 p-0 border border-slate-200 overflow-hidden bg-white param-expansion param-section"
            ).props(
                "dense "
                "header-class='bg-slate-50 text-slate-700 font-bold tracking-wide text-xs' "
                "content-class='p-0 m-0' "
                "content-style='padding:0!important;margin:0!important;'"
            )
            expansions.append(expansion)
            with expansion:
                recursive_param_editor(
                    ui.column().classes(
                        "w-full pl-2 py-1 m-0 gap-1 border-t border-slate-100"
                    ).style("padding-top:4px;padding-bottom:4px"),
                    value,
                    state,
                    task_manager,
                    columns=columns,
                    depth=depth + 1,
                    expansions=expansions,
                    pinned=pinned,
                    key_prefix=f"{key_prefix}{key}.",
                    on_pin_toggle=on_pin_toggle,
                    is_root=False,
                    full_data=full_data,
                )


def _render_pinned_block(
    full_data: Dict[str, Any],
    pinned: List[str],
    state: Dict[str, Any],
    columns: int,
    on_pin_toggle: Any,
) -> None:
    valid_pinned = []
    for pinned_key in pinned:
        parent, key, value = get_nested(full_data, pinned_key)
        if parent is not None:
            valid_pinned.append((parent, key, value, pinned_key))

    if not valid_pinned:
        return

    with ui.column().classes(PINNED_BLOCK_CLASSES):
        with ui.row().classes(PINNED_HEADER_CLASSES):
            ui.icon("push_pin", size="14px", color="indigo-500")
            ui.label("Pinned Parameters").classes(PINNED_TITLE_CLASSES)

        with _param_grid(columns, "p-0 m-0"):
            for parent, key, value, pinned_key in valid_pinned:
                _param_cell(
                    parent,
                    key,
                    value,
                    pinned_key,
                    pinned,
                    on_pin_toggle,
                    display_key=pinned_key,
                )

    with ui.row().classes("w-full items-center gap-1.5 px-0 m-0 p-0").style("min-height:20px"):
        ui.icon("list", size="14px", color="slate-400")
        ui.label("All Parameters").classes(
            "text-[11px] font-bold text-slate-400 tracking-widest leading-none p-0 m-0"
        )


def _param_cell(
    data: Dict[str, Any],
    key: str,
    value: Any,
    full_key: str,
    pinned: List[str],
    on_pin_toggle: Any = None,
    display_key: str | None = None,
) -> None:
    label = display_key or key
    type_key = _get_type_key(value)
    icon_name, icon_color, icon_bg = _TYPE_INFO[type_key]
    is_pinned = full_key in pinned

    def toggle_pin(target_key: str = full_key) -> None:
        if target_key in pinned:
            pinned.remove(target_key)
        else:
            pinned.append(target_key)
        from pyruns.utils.settings import save_setting

        save_setting("pinned_params", pinned)
        if on_pin_toggle:
            try:
                on_pin_toggle()
            except Exception:
                pass

    def on_change(event, target=data, target_key=key) -> None:
        target[target_key] = parse_value(event.value)

    card_classes = (
        "w-full px-2 py-1 gap-1 transition-colors duration-100 group param-card "
        + (
            "border border-indigo-300 bg-indigo-50/40"
            if is_pinned
            else "border border-slate-200 bg-white hover:border-slate-300"
        )
    )

    with ui.column().classes(card_classes):
        if key == "args":
            with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
                _meta_block(label, full_key, type_key, icon_name, icon_color, icon_bg)
                _pin_button(is_pinned, toggle_pin)
            ui.textarea(value=str(value), on_change=on_change).props(
                "outlined dense autogrow bg-white hide-bottom-space"
            ).classes("w-full font-mono text-[13px] text-slate-700 param-input").style(
                "min-height:84px"
            )
            return

        with ui.row().classes("w-full items-center gap-2 flex-nowrap param-row"):
            with ui.row().classes("items-center gap-2 min-w-0 shrink-0 flex-nowrap overflow-hidden param-meta"):
                _meta_block(label, full_key, type_key, icon_name, icon_color, icon_bg)

            with ui.row().classes("flex-1 min-w-0 items-center param-value-wrap"):
                if isinstance(value, bool):
                    ui.switch(value=value, on_change=on_change).props("dense color=indigo")
                elif isinstance(value, list):
                    ui.input(value=str(value), on_change=on_change).props(_TINY).classes(
                        "w-full font-mono text-[13px] text-cyan-700 param-input"
                    ).tooltip("[1, 2, 3]")
                elif isinstance(value, (int, float)):
                    ui.input(value=str(value), on_change=on_change).props(_TINY).classes(
                        "w-full font-mono text-[13px] text-blue-700 param-input"
                    )
                else:
                    ui.input(value=str(value), on_change=on_change).props(_TINY).classes(
                        "w-full font-mono text-[13px] text-slate-700 param-input"
                    )

            _pin_button(is_pinned, toggle_pin)


def _meta_block(
    label: str,
    full_key: str,
    type_key: str,
    icon_name: str,
    icon_color: str,
    icon_bg: str,
) -> None:
    with ui.element("div").classes(
        f"w-4.5 h-4.5 flex items-center justify-center {icon_bg} flex-none param-type-chip"
    ):
        ui.icon(icon_name, size="11px").classes(icon_color)
    ui.label(label).classes(
        "text-[13px] font-semibold text-slate-700 font-mono truncate whitespace-nowrap "
        "leading-none flex-1 min-w-0 param-label"
    ).tooltip(f"{full_key} ({type_key})")


def _pin_button(is_pinned: bool, on_click) -> None:
    pin_classes = (
        "text-indigo-500 opacity-100"
        if is_pinned
        else "text-slate-300 opacity-0 group-hover:opacity-60 hover:text-indigo-400"
    )
    ui.button(icon="push_pin", on_click=on_click).props(
        "flat dense round size=xs padding=0"
    ).classes(
        f"{pin_classes} min-w-0 transition-opacity duration-200 ml-auto"
    ).style("width:18px;height:18px;").tooltip(
        "Unpin" if is_pinned else "Pin this parameter"
    )


def _param_grid(columns: int, extra_classes: str = ""):
    classes = f"w-full param-grid {extra_classes}".strip()
    return ui.element("div").classes(classes).style(
        f"display:grid;grid-template-columns:repeat({columns}, minmax({_PARAM_GRID_MIN_COL_PX}px, 1fr));gap:4px;"
    )
