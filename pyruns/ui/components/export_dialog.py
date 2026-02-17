"""
Export Reports Dialog – modal for exporting monitor data as CSV/JSON.
"""
from typing import Dict, Any, List, Set

from nicegui import ui

from pyruns.utils.task_io import load_monitor_data
from pyruns.core.report import (
    build_export_csv,
    build_export_json,
    export_timestamp,
)
from pyruns.ui.theme import STATUS_ICONS, STATUS_ICON_COLORS, BTN_CLASS


def show_export_dialog(task_manager, export_ids: Set[str]) -> None:
    """Show the export dialog for tasks selected by checkbox."""
    selected_tasks = [t for t in task_manager.tasks if t["id"] in export_ids]

    if not selected_tasks:
        ui.notify(
            "No tasks selected — use checkboxes in the task list first.",
            type="warning",
            icon="warning",
        )
        return

    n_with_data = sum(1 for t in selected_tasks if load_monitor_data(t["dir"]))

    with ui.dialog() as dlg, ui.card().classes(
        "p-0 min-w-[400px] max-w-[500px] overflow-hidden shadow-2xl"
    ):
        # ── Header ──
        with ui.row().classes(
            "w-full items-center gap-2 px-4 py-3 "
            "bg-gradient-to-r from-emerald-600 to-emerald-700"
        ):
            ui.icon("download", size="20px", color="white")
            ui.label("Export Reports").classes("text-sm font-bold text-white")
            ui.space()
            ui.badge(f"{len(selected_tasks)} tasks").props(
                "color=white text-color=emerald-8"
            )

        # ── Body ──
        with ui.column().classes("px-4 py-3 gap-3"):
            # Summary stats
            with ui.row().classes("items-center gap-3"):
                with ui.column().classes("items-center gap-0"):
                    ui.label(str(len(selected_tasks))).classes(
                        "text-xl font-bold text-slate-700"
                    )
                    ui.label("Selected").classes("text-[9px] text-slate-400")
                with ui.column().classes("items-center gap-0"):
                    ui.label(str(n_with_data)).classes(
                        "text-xl font-bold text-emerald-600"
                    )
                    ui.label("With Data").classes("text-[9px] text-slate-400")

            if n_with_data == 0:
                ui.label("Selected tasks have no monitor data.").classes(
                    "text-xs text-slate-400"
                )
                ui.html(
                    '<pre style="color:#94a3b8;font-size:11px;padding:8px 12px;'
                    'background:#1e293b;border-radius:8px;margin:4px 0;">'
                    "import pyruns\n"
                    "pyruns.add_monitor(epoch=10, loss=0.234, acc=95.2)</pre>",
                    sanitize=False,
                )

            # Task name list
            with ui.column().classes("gap-1 max-h-32 overflow-auto"):
                for t in selected_tasks:
                    mon = load_monitor_data(t["dir"])
                    with ui.row().classes("items-center gap-1.5"):
                        st = t["status"]
                        ui.icon(
                            STATUS_ICONS.get(st, "help"), size="12px"
                        ).classes(STATUS_ICON_COLORS.get(st, "text-slate-400"))
                        ui.label(t.get("name", "unnamed")).classes(
                            "text-[11px] text-slate-600 truncate"
                        )
                        if mon:
                            ui.badge(f"{len(mon)}").props(
                                "color=emerald-1 text-color=emerald-8"
                            ).classes("text-[8px]")

        # ── Footer ──
        with ui.row().classes(
            "w-full justify-end gap-2 px-4 py-3 bg-slate-50 "
            "border-t border-slate-100"
        ):
            ui.button("Cancel", on_click=dlg.close).props(
                "flat no-caps"
            ).classes("text-slate-500")

            def _do_csv():
                csv_str = build_export_csv(selected_tasks)
                if not csv_str:
                    ui.notify("No data to export", type="warning")
                    return
                ts = export_timestamp()
                ui.download(
                    csv_str.encode("utf-8"), f"pyruns_report_{ts}.csv"
                )
                ui.notify("CSV exported", type="positive", icon="download")
                dlg.close()

            def _do_json():
                json_str = build_export_json(selected_tasks)
                if json_str == "[]":
                    ui.notify("No data to export", type="warning")
                    return
                ts = export_timestamp()
                ui.download(
                    json_str.encode("utf-8"), f"pyruns_report_{ts}.json"
                )
                ui.notify("JSON exported", type="positive", icon="download")
                dlg.close()

            ui.button("CSV", icon="table_chart", on_click=_do_csv).props(
                "unelevated no-caps"
            ).classes(
                f"bg-emerald-600 text-white px-4 font-bold {BTN_CLASS}"
            )
            ui.button("JSON", icon="data_object", on_click=_do_json).props(
                "unelevated no-caps"
            ).classes(
                f"bg-indigo-600 text-white px-4 font-bold {BTN_CLASS}"
            )

    dlg.open()

