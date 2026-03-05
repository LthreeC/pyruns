"""
Environment Variable Editor: key=value table with add/remove/save.
"""
from typing import Callable, Dict, List

from nicegui import ui

from pyruns.ui.theme import (
    BTN_PRIMARY,
    ENV_EDITOR_ADD_BTN_CLASSES,
    ENV_EDITOR_DEL_BTN_CLASSES,
    ENV_EDITOR_EMPTY_ADD_CLASSES,
    ENV_EDITOR_EMPTY_COL_CLASSES,
    ENV_EDITOR_EMPTY_ICON_CLASSES,
    ENV_EDITOR_EMPTY_SUB_CLASSES,
    ENV_EDITOR_EMPTY_TITLE_CLASSES,
    ENV_EDITOR_KEY_INPUT_CLASSES,
    ENV_EDITOR_ROOT_CLASSES,
    ENV_EDITOR_SAVE_BTN_CLASSES,
    ENV_EDITOR_TABLE_BODY_CLASSES,
    ENV_EDITOR_TABLE_HEAD_CLASSES,
    ENV_EDITOR_TOOLBAR_ACTIONS_CLASSES,
    ENV_EDITOR_TOOLBAR_TITLE_CLASSES,
    ENV_EDITOR_VAL_INPUT_CLASSES,
    ROW_CENTER_GAP_2,
    TOOLBAR_LIGHT,
)


def env_var_editor(
    rows: List[Dict[str, str]],
    on_save: Callable[[Dict[str, str]], None],
) -> None:
    """Render an editable env-var table."""

    def _do_save():
        on_save({r["key"]: r["val"] for r in rows if r["key"]})

    def _remove(idx: int):
        if 0 <= idx < len(rows):
            rows.pop(idx)
        _editor.refresh()

    def _add():
        rows.append({"key": "", "val": ""})
        _editor.refresh()

    @ui.refreshable
    def _editor():
        with ui.column().classes(ENV_EDITOR_ROOT_CLASSES):
            with ui.row().classes(TOOLBAR_LIGHT):
                with ui.row().classes(ROW_CENTER_GAP_2):
                    ui.icon("vpn_key", size="18px").classes("text-indigo-500")
                    ui.label(f"Environment Variables · {len(rows)}").classes(
                        ENV_EDITOR_TOOLBAR_TITLE_CLASSES
                    )
                with ui.row().classes(ENV_EDITOR_TOOLBAR_ACTIONS_CLASSES):
                    ui.button("Add", icon="add", on_click=_add).props(
                        "flat dense no-caps size=sm"
                    ).classes(ENV_EDITOR_ADD_BTN_CLASSES)
                    ui.button("Save", icon="save", on_click=_do_save).props(
                        "unelevated dense no-caps size=sm"
                    ).classes(f"{BTN_PRIMARY} {ENV_EDITOR_SAVE_BTN_CLASSES}")

            with ui.row().classes(ENV_EDITOR_TABLE_HEAD_CLASSES).style(
                "min-height: 36px; padding: 0 20px;"
            ):
                ui.label("NAME").classes(
                    "w-[35%] text-[11px] font-bold tracking-widest text-slate-500"
                )
                ui.label("VALUE").classes(
                    "flex-grow text-[11px] font-bold tracking-widest text-slate-500"
                )
                ui.element("div").classes("w-8")

            with ui.column().classes(ENV_EDITOR_TABLE_BODY_CLASSES):
                if not rows:
                    _empty_state(_add)
                    return
                for i, row in enumerate(rows):
                    _row_entry(i, row, rows, _remove)

    _editor()


def _empty_state(on_add: Callable) -> None:
    with ui.column().classes(ENV_EDITOR_EMPTY_COL_CLASSES):
        ui.icon("add_circle_outline", size="48px").classes(ENV_EDITOR_EMPTY_ICON_CLASSES)
        ui.label("No environment variables").classes(ENV_EDITOR_EMPTY_TITLE_CLASSES)
        ui.label('Click "Add" to create a new variable').classes(ENV_EDITOR_EMPTY_SUB_CLASSES)
        ui.button("Add Variable", icon="add", on_click=on_add).props(
            "outline no-caps size=sm"
        ).classes(ENV_EDITOR_EMPTY_ADD_CLASSES)


def _row_entry(
    idx: int,
    row: Dict[str, str],
    rows: List[Dict[str, str]],
    on_remove: Callable,
) -> None:
    bg = "bg-white" if idx % 2 == 0 else "bg-slate-50/50"
    with ui.row().classes(
        f"w-full items-center gap-0 {bg} border-b border-slate-100 hover:bg-indigo-50/30 transition-colors"
    ).style("min-height: 44px; padding: 0 20px;"):
        ui.input(
            value=row["key"],
            placeholder="KEY",
            on_change=lambda e, i=idx: rows[i].update({"key": e.value}),
        ).props("dense borderless").classes(ENV_EDITOR_KEY_INPUT_CLASSES)
        ui.input(
            value=row["val"],
            placeholder="VALUE",
            on_change=lambda e, i=idx: rows[i].update({"val": e.value}),
        ).props("dense borderless").classes(ENV_EDITOR_VAL_INPUT_CLASSES)
        ui.button(
            icon="close",
            on_click=lambda i=idx: on_remove(i),
        ).props("flat round dense size=xs").classes(ENV_EDITOR_DEL_BTN_CLASSES)
