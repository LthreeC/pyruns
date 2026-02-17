"""
Batch Confirmation Dialog — shown before generating multiple tasks.

Analyses the base config to distinguish product vs zip parameters
and displays a clear breakdown with value previews.
"""
from nicegui import ui
from typing import Dict, Any, List

from pyruns.utils.config_utils import flatten_dict, _parse_pipe_value


def show_batch_confirm(
    configs: List[Dict[str, Any]],
    prefix: str,
    base_config: Dict[str, Any],
    task_generator,
    task_manager,
    state: Dict[str, Any] = None,
) -> None:
    """Open a dialog that summarises the batch and lets the user confirm."""
    n = len(configs)

    # ── Classify parameters ──
    flat_base = flatten_dict(base_config)
    product_params: Dict[str, List[str]] = {}
    zip_params: Dict[str, List[str]] = {}

    for key, val in flat_base.items():
        parsed = _parse_pipe_value(val)
        if parsed is not None:
            values, mode = parsed
            (product_params if mode == "product" else zip_params)[key] = values

    product_total = 1
    for vals in product_params.values():
        product_total *= len(vals)
    zip_len = list(zip_params.values())[0] if zip_params else []
    zip_total = len(zip_len) if zip_len else 1

    # ── Dialog ──
    with ui.dialog() as dlg, ui.card().classes(
        "p-0 min-w-[520px] max-w-[680px] overflow-hidden shadow-2xl"
    ):
        _dialog_header(n)

        with ui.column().classes("px-6 py-5 gap-4"):
            _naming_preview(prefix, n)
            if product_params:
                _product_section(product_params, product_total)
            if zip_params:
                _zip_section(zip_params)
            _total_formula(product_params, zip_params, n)

        _dialog_footer(dlg, configs, prefix, n, task_generator, task_manager, state)

    dlg.open()


# ─── Dialog sub-sections ────────────────────────────────────


def _dialog_header(n: int) -> None:
    with ui.row().classes(
        "w-full items-center gap-3 px-6 py-4 "
        "bg-gradient-to-r from-indigo-600 to-indigo-700"
    ):
        ui.icon("batch_prediction", size="24px", color="white")
        ui.label("批量生成确认").classes("text-lg font-bold text-white")
        ui.space()
        ui.badge(f"{n} tasks").props("color=white text-color=indigo-8")


def _naming_preview(prefix: str, n: int) -> None:
    with ui.column().classes("gap-1.5"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("label", size="16px").classes("text-indigo-400")
            ui.label(f"任务前缀: {prefix}").classes(
                "text-sm text-slate-700 font-medium"
            )
        fmt_str = f"{prefix}-[1-of-{n}]  ~  {prefix}-[{n}-of-{n}]"
        ui.label(fmt_str).classes(
            "text-xs font-mono text-slate-500 bg-slate-50 "
            "px-3 py-1.5 border border-slate-100"
        )


def _param_row_list(params: Dict[str, List[str]], color: str) -> None:
    """Render parameter key → values rows for product or zip sections."""
    for key, vals in params.items():
        with ui.row().classes("items-center gap-2"):
            ui.label(key).classes(
                f"text-[11px] font-mono font-bold text-{color}-600 "
                "min-w-[120px] truncate"
            ).tooltip(key)
            display = vals[:10]
            if len(vals) > 10:
                display.append(f"…+{len(vals) - 10}")
            ui.label(" | ".join(display)).classes(
                "text-[11px] font-mono text-slate-600 truncate"
            )


def _product_section(product_params: Dict[str, List[str]], product_total: int) -> None:
    with ui.column().classes("gap-2"):
        with ui.row().classes("items-center gap-1.5"):
            ui.icon("shuffle", size="14px").classes("text-indigo-500")
            ui.label("Product 参数 (笛卡尔积)").classes(
                "text-[11px] font-bold text-indigo-600 uppercase tracking-wider"
            )
            ui.badge(
                " × ".join(str(len(v)) for v in product_params.values())
                + f" = {product_total}"
            ).props("color=indigo-1 text-color=indigo-8").classes("text-[10px]")
        with ui.column().classes(
            "gap-1 max-h-[140px] overflow-auto "
            "bg-indigo-50/50 px-3 py-2 border border-indigo-100"
        ):
            _param_row_list(product_params, "indigo")


def _zip_section(zip_params: Dict[str, List[str]]) -> None:
    zip_count = len(list(zip_params.values())[0])
    with ui.column().classes("gap-2"):
        with ui.row().classes("items-center gap-1.5"):
            ui.icon("link", size="14px").classes("text-purple-500")
            ui.label("Zip 参数 (配对组合)").classes(
                "text-[11px] font-bold text-purple-600 uppercase tracking-wider"
            )
            ui.badge(f"× {zip_count}").props(
                "color=purple-1 text-color=purple-8"
            ).classes("text-[10px]")
        with ui.column().classes(
            "gap-1 max-h-[140px] overflow-auto "
            "bg-purple-50/50 px-3 py-2 border border-purple-100"
        ):
            _param_row_list(zip_params, "purple")


def _total_formula(
    product_params: Dict[str, List[str]],
    zip_params: Dict[str, List[str]],
    n: int,
) -> None:
    with ui.row().classes(
        "items-center gap-2 bg-slate-50 px-4 py-2 border border-slate-200"
    ):
        ui.icon("calculate", size="16px").classes("text-slate-500")
        parts = []
        if product_params:
            parts.append(" × ".join(str(len(v)) for v in product_params.values()))
        if zip_params:
            parts.append(str(len(list(zip_params.values())[0])))
        formula = " × ".join(parts) if parts else "1"
        ui.label(f"Total = {formula} = {n}").classes(
            "text-xs font-mono font-bold text-slate-700"
        )


def _dialog_footer(dlg, configs, prefix, n, task_generator, task_manager, state=None) -> None:
    with ui.row().classes(
        "w-full justify-end gap-3 px-6 py-4 bg-slate-50 border-t border-slate-100"
    ):
        ui.button("取消", on_click=dlg.close).props("flat no-caps").classes(
            "text-slate-500"
        )

        def do_generate():
            tasks = task_generator.create_tasks(configs, prefix)
            task_manager.add_tasks(tasks)
            if state is not None:
                state["_manager_dirty"] = True
            ui.notify(
                f"Generated {n} tasks: {prefix}-[1-of-{n}] ~ {prefix}-[{n}-of-{n}]",
                type="positive",
                icon="grid_view",
            )
            dlg.close()

        ui.button(
            f"确认生成 {n} 个任务",
            icon="rocket_launch",
            on_click=do_generate,
        ).props("unelevated no-caps").classes(
            "bg-indigo-600 text-white px-5 rounded-lg font-bold"
        )

