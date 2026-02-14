"""
Environment Variable Editor — PyCharm-style key=value table with add/remove/save.
"""
from nicegui import ui
from typing import Dict, List, Callable


def env_var_editor(
    rows: List[Dict[str, str]],
    on_save: Callable[[Dict[str, str]], None],
) -> None:
    """Render an editable env-var table.

    Parameters
    ----------
    rows : list of {"key": str, "val": str}
        Mutable list of environment variable entries.
    on_save : callable
        Called with ``{key: val}`` dict when the user clicks Save.
    """

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
        with ui.column().classes("w-full h-full gap-0"):

            # ── Toolbar ──
            with ui.row().classes(
                "w-full items-center justify-between px-5 py-2.5 flex-none "
                "bg-gradient-to-r from-indigo-50 to-slate-50 "
                "border-b border-indigo-100"
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

            # ── Table header ──
            with ui.row().classes(
                "w-full items-center bg-slate-100 gap-0 flex-none "
                "border-b border-slate-200"
            ).style("min-height: 36px; padding: 0 20px;"):
                ui.label("NAME").classes(
                    "w-[35%] text-[11px] font-bold tracking-widest text-slate-500"
                )
                ui.label("VALUE").classes(
                    "flex-grow text-[11px] font-bold tracking-widest text-slate-500"
                )
                ui.element("div").classes("w-8")

            # ── Table body ──
            with ui.column().classes(
                "w-full gap-0 flex-grow overflow-auto bg-white"
            ):
                if not rows:
                    _empty_state(_add)
                    return

                for i, row in enumerate(rows):
                    _row_entry(i, row, rows, _remove)

    _editor()


def _empty_state(on_add: Callable) -> None:
    """Placeholder when no variables are defined."""
    with ui.column().classes(
        "w-full items-center justify-center py-20 gap-4"
    ):
        ui.icon("add_circle_outline", size="48px").classes("text-indigo-200")
        ui.label("No environment variables").classes(
            "text-slate-400 text-base"
        )
        ui.label('Click "Add" to create a new variable').classes(
            "text-slate-300 text-xs"
        )
        ui.button("Add Variable", icon="add", on_click=on_add).props(
            "outline no-caps size=sm"
        ).classes("text-indigo-500 border-indigo-300 mt-2 px-4")


def _row_entry(
    idx: int,
    row: Dict[str, str],
    rows: List[Dict[str, str]],
    on_remove: Callable,
) -> None:
    bg = "bg-white" if idx % 2 == 0 else "bg-slate-50/50"
    with ui.row().classes(
        f"w-full items-center gap-0 {bg} "
        "border-b border-slate-100 hover:bg-indigo-50/30 transition-colors"
    ).style("min-height: 44px; padding: 0 20px;"):
        ui.input(
            value=row["key"], placeholder="KEY",
            on_change=lambda e, i=idx: rows[i].update({"key": e.value}),
        ).props("dense borderless").classes(
            "w-[35%] font-mono text-[13px] text-slate-800"
        )
        ui.input(
            value=row["val"], placeholder="VALUE",
            on_change=lambda e, i=idx: rows[i].update({"val": e.value}),
        ).props("dense borderless").classes(
            "flex-grow font-mono text-[13px] text-slate-700"
        )
        ui.button(
            icon="close", on_click=lambda i=idx: on_remove(i),
        ).props("flat round dense size=xs").classes(
            "w-8 text-slate-300 hover:text-red-500 transition-all"
        )

