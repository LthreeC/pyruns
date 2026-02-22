"""
Export Reports Dialog – modal for exporting monitor data as CSV/JSON.
"""
from typing import Set

from nicegui import ui

from pyruns.utils.task_io import load_monitor_data
from pyruns.core.report import (
    build_export_csv,
    build_export_json,
    export_timestamp,
)
from pyruns.ui.theme import STATUS_ICONS, STATUS_ICON_COLORS


async def show_export_dialog(task_manager, export_ids: Set[str]) -> None:
    """Show the export dialog for tasks selected by checkbox."""
    selected_tasks = [t for t in task_manager.tasks if t["name"] in export_ids]
    
    if not selected_tasks:
        ui.notify(
            "No tasks selected — use checkboxes in the task list first.",
            type="warning",
            icon="warning",
        )
        return

    with ui.dialog() as dlg, ui.card().classes(
        "p-0 min-w-[400px] max-w-[500px] overflow-hidden shadow-2xl"
    ):
        # ── Header ──
        from pyruns.ui.theme import PANEL_HEADER_INDIGO
        with ui.row().classes(
            f"w-full items-center gap-2 px-4 py-3 {PANEL_HEADER_INDIGO}"
        ):
            ui.icon("download", size="20px", color="white")
            ui.label("Export Reports").classes("text-sm font-bold text-white")
            ui.space()

        # ── Body (Loading Spinner & Skeleton) ──
        body_container = ui.column().classes("px-4 py-3 gap-3 w-full")
        with body_container:
            with ui.row().classes("w-full items-center justify-center p-4 gap-2"):
                ui.spinner("dots", size="lg", color="indigo")
                ui.label("Scanning monitor logs...").classes("text-slate-500 font-bold")

        dlg.open()
        
        # Async Data Parsing Off Thread
        from nicegui import run
        def _scan_logs():
            tasks_with_data = []
            for t in selected_tasks:
                mon = load_monitor_data(t["dir"])
                if mon:
                    tasks_with_data.append((t, mon))
                else:
                    tasks_with_data.append((t, None))
            return tasks_with_data

        fetched_results = await run.io_bound(_scan_logs)
        n_with_data = sum(1 for _, mon in fetched_results if mon)

        body_container.clear()
        
        with body_container:
            # Summary stats
            with ui.row().classes("items-center gap-3"):
                with ui.column().classes("items-center gap-0"):
                    ui.label(str(len(selected_tasks))).classes(
                        "text-xl font-bold text-slate-700"
                    )
                    ui.label("Selected").classes("text-[9px] text-slate-400")
                with ui.column().classes("items-center gap-0"):
                    ui.label(str(n_with_data)).classes(
                        "text-xl font-bold text-indigo-600"
                    )
                    ui.label("With Data").classes("text-[9px] text-slate-400")

            if n_with_data == 0:
                from pyruns.ui.theme import EXPORT_PRE_STYLE
                ui.label("Selected tasks have no monitor data.").classes(
                    "text-xs text-slate-400"
                )
                ui.html(
                    f'<pre style="{EXPORT_PRE_STYLE}">'
                    "import pyruns\n"
                    "pyruns.add_monitor(epoch=10, loss=0.234, acc=95.2)</pre>",
                    sanitize=False,
                )

            # Task name list (wider so scrollbar is on the right)
            with ui.scroll_area().classes("w-full max-h-[40vh] border border-slate-100 rounded bg-slate-50/50"):
                with ui.column().classes("w-full gap-1 p-2"):
                    for t, mon in fetched_results:
                        with ui.row().classes("w-full items-center gap-1.5 flex-nowrap overflow-hidden"):
                            st = t["status"]
                            ui.icon(
                                STATUS_ICONS.get(st, "help"), size="12px"
                            ).classes(STATUS_ICON_COLORS.get(st, "text-slate-400") + " shrink-0")
                            ui.label(t.get("name", "unnamed")).classes(
                                "text-[11px] text-slate-600 truncate flex-grow min-w-0"
                            )
                            if mon:
                                ui.badge(f"{len(mon)} pts").props(
                                    "color=indigo-1 text-color=indigo-8"
                                ).classes("text-[9px] font-mono shrink-0")

        # ── Footer ──
        with ui.row().classes(
            "w-full justify-between items-center gap-2 px-4 py-3 bg-slate-50 "
            "border-t border-slate-100"
        ):
            with ui.row().classes("items-center gap-2"):
                csv_btn = ui.button("CSV", on_click=lambda: _do_csv()).props(
                    "unelevated color=indigo-600 text-white"
                ).classes("font-bold shadow-sm")
                if n_with_data == 0:
                    csv_btn.props(add="disable")

                json_btn = ui.button("JSON", on_click=lambda: _do_json()).props(
                    "unelevated color=indigo-600 text-white"
                ).classes("font-bold shadow-sm")
                if n_with_data == 0:
                    json_btn.props(add="disable")

                spinner = ui.spinner("dots", size="lg", color="indigo").classes("hidden ml-2")
            
            cancel_btn = ui.button("Cancel", on_click=dlg.close).props(
                "flat no-caps color=red-500"
            ).classes("font-bold px-4")

            async def _do_csv():
                from nicegui import run
                spinner.classes(remove="hidden")
                csv_btn.props(add="disable")
                json_btn.props(add="disable")
                
                csv_str = await run.io_bound(build_export_csv, selected_tasks)
                
                spinner.classes(add="hidden")
                csv_btn.props(remove="disable")
                json_btn.props(remove="disable")

                if not csv_str:
                    ui.notify("No data to export", type="warning")
                    return
                ts = export_timestamp()
                ui.download(
                    csv_str.encode("utf-8"), f"pyruns_report_{ts}.csv"
                )
                ui.notify("CSV exported", type="positive", icon="download")
                dlg.close()

            async def _do_json():
                from nicegui import run
                spinner.classes(remove="hidden")
                csv_btn.props(add="disable")
                json_btn.props(add="disable")
                
                json_str = await run.io_bound(build_export_json, selected_tasks)
                
                spinner.classes(add="hidden")
                csv_btn.props(remove="disable")
                json_btn.props(remove="disable")

                if json_str == "[]":
                    ui.notify("No data to export", type="warning")
                    return
                ts = export_timestamp()
                ui.download(
                    json_str.encode("utf-8"), f"pyruns_report_{ts}.json"
                )
                ui.notify("JSON exported", type="positive", icon="download")
                dlg.close()

