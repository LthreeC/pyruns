"""
Generator Page – load config templates, edit parameters, generate tasks.
Supports Form view (compact structured editor) and YAML view (raw editor).

Batch syntax (per-parameter):
    param: val1 | val2 | val3        →  product (笛卡尔积)
    param: (val1 | val2 | val3)      →  zip (配对, 各zip参数长度须一致)
Total = ∏(product参数数量) × zip长度
"""
import os
import datetime
import yaml
from nicegui import ui
from typing import Dict, Any

from pyruns.utils.config_utils import (
    load_yaml, generate_batch_configs, flatten_dict,
    list_template_files, strip_batch_pipes,
    _parse_pipe_value,
)
from pyruns.ui.components.param_editor import recursive_param_editor
from pyruns.ui.theme import INPUT_PROPS, BTN_CLASS
from pyruns.ui.widgets import dir_picker, _ensure_css
from pyruns.utils import get_logger

logger = get_logger(__name__)


def render_generator_page(state: Dict[str, Any], task_generator, task_manager) -> None:
    """Page 1: Task Generator (Side-by-Side)"""

    _ensure_css()

    # ── Local view state ──
    view_mode = {"current": "form"}  # "form" | "yaml"
    form_cols = {"n": 2}  # default 2 columns
    yaml_holder = {"text": ""}
    expansions_ref = []  # filled by recursive_param_editor each refresh

    # ══════════════════════════════════════════════════════════════
    #  Row 1: Config Loading Bar
    # ══════════════════════════════════════════════════════════════
    with ui.row().classes(
        "w-full items-center gap-4 mb-6 bg-white p-4 "
        "rounded-xl shadow-sm border border-slate-100"
    ):
        tasks_dir_input = dir_picker(
            value=state["tasks_dir"],
            label="Tasks Root",
            on_change=lambda path: (state.update({"tasks_dir": path}), refresh_files()),
            input_classes="flex-grow",
        )

        file_select = ui.select([], label="Template").props(INPUT_PROPS).classes("w-64")

        # Read-only indicator for config_default.yaml
        with ui.row().classes("items-center gap-1"):
            tpl_lock = ui.icon("lock", size="16px").classes("text-slate-300")
            tpl_lock.tooltip("模板只读 — 编辑不会修改 config_default.yaml 原始文件")
            tpl_lock.set_visibility(False)

        def on_file_select_change() -> None:
            if not file_select.value:
                return
            val = file_select.value
            if val.startswith(".."):
                path = os.path.abspath(os.path.join(state["tasks_dir"], val))
            else:
                path = os.path.join(state["tasks_dir"], val)

            # Show lock icon for config_default.yaml
            tpl_lock.set_visibility("config_default" in val)

            if os.path.exists(path):
                config_data = load_yaml(path)
                state["config_data"] = config_data
                state["config_path"] = path
                yaml_holder["text"] = _dict_to_yaml(config_data)
                try:
                    editor_area.refresh()
                except NameError:
                    pass

        file_select.on_value_change(on_file_select_change)

        def refresh_files() -> None:
            state["tasks_dir"] = tasks_dir_input.value
            options = list_template_files(state["tasks_dir"])
            file_select.options = options
            if options and not file_select.value:
                file_select.value = list(options.keys())[0]
            if file_select.value:
                on_file_select_change()

        ui.button(icon="refresh", on_click=refresh_files).props("flat round color=slate")

        if not file_select.options:
            refresh_files()

    # ══════════════════════════════════════════════════════════════
    #  Row 2: Main Content (Side-by-Side)
    # ══════════════════════════════════════════════════════════════
    with ui.row().classes("w-full gap-6 flex-nowrap items-start"):

        # ── LEFT: Parameter Editor ──
        with ui.column().classes("flex-grow min-w-0"):

            @ui.refreshable
            def editor_area() -> None:
                config_data = state.get("config_data")
                if not config_data and file_select.value:
                    on_file_select_change()
                    config_data = state.get("config_data")

                if not config_data:
                    with ui.column().classes("w-full items-center justify-center py-20"):
                        ui.icon("description", size="48px").classes("text-slate-200")
                        ui.label("No config loaded").classes("text-slate-400 italic mt-2")
                    return

                # ── Toolbar: view toggle + column selector ──
                with ui.row().classes(
                    "w-full items-center justify-between px-4 py-2.5 mb-3 "
                    "bg-white rounded-xl border border-slate-100 shadow-sm"
                ):
                    # Left: title
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("tune", size="20px").classes("text-indigo-500")
                        ui.label("Parameters").classes(
                            "text-sm font-bold text-slate-700 tracking-wide"
                        )

                    # Right: controls
                    with ui.row().classes("items-center gap-2"):
                        # Form-only controls
                        if view_mode["current"] == "form":
                            # Expand / Collapse all
                            ui.button(
                                icon="unfold_more",
                                on_click=lambda: [e.set_value(True) for e in expansions_ref],
                            ).props("flat dense round size=sm").classes(
                                "text-slate-400 hover:text-indigo-500"
                            ).tooltip("Expand all")
                            ui.button(
                                icon="unfold_less",
                                on_click=lambda: [e.set_value(False) for e in expansions_ref],
                            ).props("flat dense round size=sm").classes(
                                "text-slate-400 hover:text-indigo-500"
                            ).tooltip("Collapse all")

                            # Column selector
                            ui.select(
                                {n: f"{n} cols" for n in range(1, 10)},
                                value=form_cols["n"],
                            ).props(
                                "dense outlined rounded options-dense"
                            ).classes("w-24").on_value_change(
                                lambda e: _set_cols(e.value, form_cols, editor_area)
                            )

                        # View mode toggle
                        with ui.row().classes(
                            "items-center gap-1 bg-slate-100 rounded-lg p-1"
                        ):
                            _toggle_btn(
                                "Form", "view_list",
                                view_mode["current"] == "form",
                                lambda: _switch_view("form", state, yaml_holder, view_mode, editor_area),
                            )
                            _toggle_btn(
                                "YAML", "code",
                                view_mode["current"] == "yaml",
                                lambda: _switch_view("yaml", state, yaml_holder, view_mode, editor_area),
                            )

                # ── Content ──
                if view_mode["current"] == "form":
                    expansions_ref.clear()
                    recursive_param_editor(
                        ui.column().classes("w-full gap-0"),
                        config_data, state, task_manager,
                        columns=form_cols["n"],
                        expansions=expansions_ref,
                        on_star_toggle=lambda: editor_area.refresh(),
                    )
                else:
                    _render_yaml_view(yaml_holder)

            editor_area()
            _editor_area_ref[0] = editor_area

        # ── RIGHT: Settings & Generate Button ──
        with ui.column().classes("w-96 flex-none sticky top-4"):
            with ui.card().classes(
                "w-full p-6 shadow-md rounded-2xl bg-white border border-slate-100"
            ):
                ui.label("Generation Settings").classes(
                    "text-lg font-bold mb-4 text-slate-800"
                )

                ui.input(
                    value=state["task_name_input"],
                    placeholder="e.g. baseline-run",
                    label="Task Name (= Folder Name)",
                ).props(INPUT_PROPS).classes("w-full mb-2").on_value_change(
                    lambda e: state.update({"task_name_input": e.value})
                )

                # Timestamp auto-naming checkbox
                use_ts_chk = ui.checkbox(
                    "名称为空时自动使用时间戳命名", value=True,
                ).props("dense color=indigo").classes("text-xs text-slate-500 mb-5")

                # ── Batch syntax hint ──
                with ui.column().classes(
                    "w-full gap-1.5 mb-5 px-3 py-2.5 "
                    "bg-slate-50 rounded-lg border border-slate-100"
                ):
                    with ui.row().classes("items-center gap-1.5"):
                        ui.icon("info_outline", size="15px").classes("text-indigo-400")
                        ui.label("批量语法").classes(
                            "text-[11px] font-bold text-slate-600"
                        )
                    ui.html(
                        '<span style="font-family:monospace;font-size:10px;color:#475569;">'
                        '<b style="color:#6366f1;">Product</b>: &nbsp;'
                        '<code style="background:#e0e7ff;padding:1px 4px;border-radius:3px;">'
                        'lr: 0.001 | 0.01 | 0.1</code><br>'
                        '<b style="color:#8b5cf6;">Zip</b>: &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;'
                        '<code style="background:#ede9fe;padding:1px 4px;border-radius:3px;">'
                        'seed: (1 | 2 | 3)</code><br>'
                        '<span style="color:#94a3b8;">Total = ∏(product) × zip_len</span>'
                        '</span>',
                        sanitize=False,
                    )

                ui.separator().classes("mb-4")

                def handle_generate() -> None:
                    # YAML 模式: 先解析并校验
                    if view_mode["current"] == "yaml":
                        ok = _sync_yaml_to_config(yaml_holder["text"], state)
                        if not ok:
                            return

                    if not state.get("config_data"):
                        ui.notify("Load a config first!", type="warning")
                        return

                    # Resolve task name
                    prefix = state["task_name_input"].strip()
                    if not prefix:
                        if use_ts_chk.value:
                            prefix = f"task_{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
                        else:
                            ui.notify(
                                "请输入任务名称，或勾选自动时间戳命名",
                                type="warning", icon="warning",
                            )
                            return

                    from pyruns.utils.task_utils import validate_task_name
                    err = validate_task_name(prefix)
                    if err:
                        ui.notify(f"Invalid task name: {err}", type="negative", icon="error")
                        return

                    # Auto-detect pipe syntax → batch generate
                    try:
                        configs = generate_batch_configs(state["config_data"])
                    except ValueError as e:
                        ui.notify(str(e), type="negative", icon="error")
                        return

                    n = len(configs)
                    if n == 1:
                        # Single task → generate directly
                        tasks = task_generator.create_tasks(configs, prefix)
                        task_manager.add_tasks(tasks)
                        ui.notify("Generated 1 task", type="positive", icon="add_circle")
                    else:
                        # Multiple tasks → show confirmation dialog
                        _show_batch_confirm(
                            configs, prefix, state["config_data"],
                            task_generator, task_manager,
                        )

                ui.button(
                    "GENERATE", on_click=handle_generate,
                ).props("unelevated icon=rocket_launch").classes(
                    f"w-full bg-indigo-600 text-white font-bold tracking-wide "
                    f"rounded-lg py-3.5 hover:bg-indigo-700 shadow-lg hover:shadow-xl "
                    f"{BTN_CLASS} text-base"
                )

                ui.label(
                    '无 | 语法 → 直接生成  ·  有 | 语法 → 确认后批量生成'
                ).classes("text-[10px] text-slate-400 mt-3 text-center w-full")


# ═══════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════

# Module-level ref so param_editor can trigger a refresh for star toggle
_editor_area_ref = [None]


def _set_cols(n: int, form_cols: dict, editor_area):
    """Update column count and refresh."""
    form_cols["n"] = n
    editor_area.refresh()


def _toggle_btn(label: str, icon: str, active: bool, on_click):
    """Prominent segmented toggle button with icon."""
    if active:
        ui.button(label, icon=icon, on_click=on_click).props(
            "unelevated no-caps size=md"
        ).classes(
            "bg-indigo-600 text-white font-bold px-5 py-1.5 rounded-lg "
            "shadow-md text-sm tracking-wide"
        )
    else:
        ui.button(label, icon=icon, on_click=on_click).props(
            "flat no-caps size=md"
        ).classes(
            "text-slate-500 font-medium px-5 py-1.5 rounded-lg text-sm "
            "hover:text-indigo-600 hover:bg-indigo-50/50"
        )


def _switch_view(target, state, yaml_holder, view_mode, editor_area):
    if view_mode["current"] == target:
        return

    if target == "yaml":
        # Form → YAML: serialize current data
        yaml_holder["text"] = _dict_to_yaml(state.get("config_data", {}))
    else:
        # YAML → Form: best-effort sync (no blocking validation)
        try:
            parsed = yaml.safe_load(yaml_holder["text"])
            if isinstance(parsed, dict):
                state["config_data"] = parsed
        except yaml.YAMLError:
            pass  # keep existing config_data, user can fix later

    view_mode["current"] = target
    editor_area.refresh()


def _dict_to_yaml(data: Dict[str, Any]) -> str:
    if not data:
        return ""
    clean = {k: v for k, v in data.items() if not k.startswith("_meta")}
    return yaml.dump(clean, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _sync_yaml_to_config(
    yaml_text: str, state: Dict[str, Any], *, quiet: bool = False
) -> bool:
    """Parse YAML text back into state['config_data']. Returns True on success."""
    try:
        parsed = yaml.safe_load(yaml_text)
        if parsed is None:
            if not quiet:
                ui.notify("YAML is empty", type="warning", icon="warning")
            return False
        if not isinstance(parsed, dict):
            if not quiet:
                ui.notify("YAML must be a mapping (key: value)", type="negative", icon="error")
            return False
        state["config_data"] = parsed
        return True
    except yaml.YAMLError as e:
        if not quiet:
            msg = str(e)
            if len(msg) > 120:
                msg = msg[:120] + "..."
            ui.notify(f"YAML syntax error:\n{msg}", type="negative", icon="error", multi_line=True)
        return False


def _render_yaml_view(yaml_holder):
    """Render a clean YAML editor — just the CodeMirror, no extra chrome."""
    cm = ui.codemirror(
        value=yaml_holder["text"],
        language="YAML",
        theme="vscodeDark",
        line_wrapping=True,
    ).classes("w-full rounded-xl overflow-hidden").style(
        "height: calc(100vh - 260px); min-height: 400px;"
    )
    cm.on_value_change(lambda e: yaml_holder.update({"text": e.value}))


# ═══════════════════════════════════════════════════════════════
#  Batch Confirmation Dialog
# ═══════════════════════════════════════════════════════════════

def _show_batch_confirm(configs, prefix, base_config, task_generator, task_manager):
    """Show a confirmation dialog before generating multiple tasks.

    Analyses original base_config to distinguish product vs zip parameters
    and displays a clear breakdown with value previews.
    """
    n = len(configs)

    # ── Analyse base_config to classify parameters ──
    flat_base = flatten_dict(base_config)
    product_params = {}  # key → [values]
    zip_params = {}      # key → [values]
    for k, v in flat_base.items():
        parsed = _parse_pipe_value(v)
        if parsed is not None:
            values, mode = parsed
            if mode == "product":
                product_params[k] = values
            else:
                zip_params[k] = values

    # Compute totals for display
    product_total = 1
    for vals in product_params.values():
        product_total *= len(vals)
    zip_len = list(zip_params.values())[0] if zip_params else []
    zip_total = len(zip_len) if zip_len else 1

    with ui.dialog() as dlg, ui.card().classes(
        "p-0 min-w-[520px] max-w-[680px] rounded-2xl overflow-hidden shadow-2xl"
    ):
        # ── Header ──
        with ui.row().classes(
            "w-full items-center gap-3 px-6 py-4 "
            "bg-gradient-to-r from-indigo-600 to-indigo-700"
        ):
            ui.icon("batch_prediction", size="24px", color="white")
            ui.label("批量生成确认").classes("text-lg font-bold text-white")
            ui.space()
            ui.badge(f"{n} tasks").props("color=white text-color=indigo-8 rounded")

        # ── Body ──
        with ui.column().classes("px-6 py-5 gap-4"):
            # Naming preview
            with ui.column().classes("gap-1.5"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("label", size="16px").classes("text-indigo-400")
                    ui.label(f"任务前缀: {prefix}").classes("text-sm text-slate-700 font-medium")
                fmt_str = f"{prefix}-[1-of-{n}]  ~  {prefix}-[{n}-of-{n}]"
                ui.label(fmt_str).classes(
                    "text-xs font-mono text-slate-500 bg-slate-50 "
                    "px-3 py-1.5 rounded-md border border-slate-100"
                )

            # ── Product parameters ──
            if product_params:
                with ui.column().classes("gap-2"):
                    with ui.row().classes("items-center gap-1.5"):
                        ui.icon("shuffle", size="14px").classes("text-indigo-500")
                        ui.label("Product 参数 (笛卡尔积)").classes(
                            "text-[11px] font-bold text-indigo-600 uppercase tracking-wider"
                        )
                        ui.badge(
                            " × ".join(str(len(v)) for v in product_params.values())
                            + f" = {product_total}"
                        ).props("color=indigo-1 text-color=indigo-8 rounded").classes("text-[10px]")
                    with ui.column().classes(
                        "gap-1 max-h-[140px] overflow-auto "
                        "bg-indigo-50/50 rounded-lg px-3 py-2 border border-indigo-100"
                    ):
                        for k, vals in product_params.items():
                            with ui.row().classes("items-center gap-2"):
                                ui.label(k).classes(
                                    "text-[11px] font-mono font-bold text-indigo-600 "
                                    "min-w-[120px] truncate"
                                ).tooltip(k)
                                display_vals = vals[:10]
                                if len(vals) > 10:
                                    display_vals.append(f"…+{len(vals) - 10}")
                                ui.label(" | ".join(display_vals)).classes(
                                    "text-[11px] font-mono text-slate-600 truncate"
                                )

            # ── Zip parameters ──
            if zip_params:
                zip_count = len(list(zip_params.values())[0])
                with ui.column().classes("gap-2"):
                    with ui.row().classes("items-center gap-1.5"):
                        ui.icon("link", size="14px").classes("text-purple-500")
                        ui.label("Zip 参数 (配对组合)").classes(
                            "text-[11px] font-bold text-purple-600 uppercase tracking-wider"
                        )
                        ui.badge(f"× {zip_count}").props(
                            "color=purple-1 text-color=purple-8 rounded"
                        ).classes("text-[10px]")
                    with ui.column().classes(
                        "gap-1 max-h-[140px] overflow-auto "
                        "bg-purple-50/50 rounded-lg px-3 py-2 border border-purple-100"
                    ):
                        for k, vals in zip_params.items():
                            with ui.row().classes("items-center gap-2"):
                                ui.label(k).classes(
                                    "text-[11px] font-mono font-bold text-purple-600 "
                                    "min-w-[120px] truncate"
                                ).tooltip(k)
                                display_vals = vals[:10]
                                if len(vals) > 10:
                                    display_vals.append(f"…+{len(vals) - 10}")
                                ui.label(" | ".join(display_vals)).classes(
                                    "text-[11px] font-mono text-slate-600 truncate"
                                )

            # ── Total formula ──
            with ui.row().classes(
                "items-center gap-2 bg-slate-50 rounded-lg px-4 py-2 border border-slate-200"
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

        # ── Footer ──
        with ui.row().classes(
            "w-full justify-end gap-3 px-6 py-4 bg-slate-50 border-t border-slate-100"
        ):
            ui.button("取消", on_click=dlg.close).props(
                "flat no-caps"
            ).classes("text-slate-500")

            def do_gen():
                tasks = task_generator.create_tasks(configs, prefix)
                task_manager.add_tasks(tasks)
                ui.notify(
                    f"Generated {n} tasks: {prefix}-[1-of-{n}] ~ {prefix}-[{n}-of-{n}]",
                    type="positive", icon="grid_view",
                )
                dlg.close()

            ui.button(
                f"确认生成 {n} 个任务", icon="rocket_launch",
                on_click=do_gen,
            ).props("unelevated no-caps").classes(
                "bg-indigo-600 text-white px-5 rounded-lg font-bold"
            )

    dlg.open()
