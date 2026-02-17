"""
Generator Page — load config templates, edit parameters, generate tasks.

Supports Form view (compact structured editor) and YAML view (raw editor).

Batch syntax (per-parameter):
    param: val1 | val2 | val3        →  product (cartesian)
    param: (val1 | val2 | val3)      →  zip (paired, lengths must match)
Total = ∏(product counts) × zip_length
"""
import os
import yaml
from nicegui import ui
from typing import Dict, Any

from pyruns.utils.config_utils import (
    load_yaml, generate_batch_configs,
    list_template_files, strip_batch_pipes,
)
from pyruns.utils import get_logger, get_now_str
from pyruns.ui.components.param_editor import recursive_param_editor
from pyruns.ui.components.batch_dialog import show_batch_confirm
from pyruns.ui.theme import INPUT_PROPS, BTN_CLASS
from pyruns.ui.widgets import dir_picker, _ensure_css
from pyruns.utils import get_logger

logger = get_logger(__name__)

# Module-level ref so param_editor can trigger a refresh for star toggle
_editor_area_ref = [None]


def render_generator_page(
    state: Dict[str, Any], task_generator, task_manager
) -> None:
    """Entry point for the Generator tab."""
    _ensure_css()

    # ── Per-session view state (column count from settings) ──
    from pyruns.utils.settings import get as _get_setting
    view_mode = {"current": "form"}
    form_cols = {"n": int(_get_setting("generator_form_columns", 2))}
    yaml_holder = {"text": ""}
    expansions_ref: list = []

    # ══════════════════════════════════════════════════════════
    #  Row 1 — Config loading bar
    # ══════════════════════════════════════════════════════════
    with ui.row().classes(
        "w-full items-center gap-2 mb-2 px-3 py-1.5 bg-white "
        "border-b border-slate-200 sticky top-0 z-10 shadow-sm"
    ):
        tasks_dir_input = dir_picker(
            value=state["tasks_dir"],
            label="Tasks Root",
            on_change=lambda path: (
                state.update({"tasks_dir": path}),
                refresh_files(),
            ),
            input_classes="flex-grow",
        )

        file_select = ui.select(
            [], label="Template"
        ).props(INPUT_PROPS).classes("w-64")

        # Read-only indicator for config_default.yaml
        with ui.row().classes("items-center gap-1"):
            tpl_lock = ui.icon("lock", size="16px").classes("text-slate-300")
            tpl_lock.tooltip(
                "模板只读 — 编辑不会修改 config_default.yaml 原始文件"
            )
            tpl_lock.set_visibility(False)

        def on_file_select_change() -> None:
            if not file_select.value:
                return
            val = file_select.value
            if val.startswith(".."):
                path = os.path.abspath(
                    os.path.join(state["tasks_dir"], val)
                )
            else:
                path = os.path.join(state["tasks_dir"], val)

            tpl_lock.set_visibility("config_default" in val)

            if os.path.exists(path):
                config_data = load_yaml(path)
                state["config_data"] = config_data
                state["config_path"] = path
                yaml_holder["text"] = _dict_to_yaml(config_data)
                logger.debug("Loaded template: %s", val)
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

        ui.button(
            icon="refresh", on_click=refresh_files
        ).props("flat round dense color=slate")

        if not file_select.options:
            refresh_files()

    # ══════════════════════════════════════════════════════════
    #  Row 2 — Main Workspace (Side-by-side)
    # ══════════════════════════════════════════════════════════
    # 使用 h-[calc...] 确保在大屏上最大化利用空间
    with ui.row().classes("w-full gap-1 flex-nowrap items-start h-full"):

        # ── LEFT: Parameter editor (Flexible Width) ──
        with ui.column().classes("flex-grow min-w-0 h-full"):

            @ui.refreshable
            def editor_area() -> None:
                config_data = state.get("config_data")
                if not config_data and file_select.value:
                    on_file_select_change()
                    config_data = state.get("config_data")

                if not config_data:
                    _empty_editor_placeholder()
                    return

                _editor_toolbar(
                    view_mode, form_cols, expansions_ref,
                    state, yaml_holder, editor_area,
                )

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

        # ── RIGHT: Settings & generate button ──
        with ui.column().classes("w-96 flex-none sticky top-4"):
            _settings_panel(
                state, view_mode, yaml_holder,
                task_generator, task_manager,
            )


# ═══════════════════════════════════════════════════════════
#  Editor toolbar (view toggle + column selector)
# ═══════════════════════════════════════════════════════════


def _editor_toolbar(
    view_mode, form_cols, expansions_ref,
    state, yaml_holder, editor_area,
) -> None:
    with ui.row().classes(
        "w-full items-center justify-between px-3 py-1 mb-0 "
        "bg-white border-b border-slate-200 shadow-sm"
    ):
        with ui.row().classes("items-center gap-2"):
            ui.icon("tune", size="20px").classes("text-indigo-500")
            ui.label("Parameters").classes(
                "text-sm font-bold text-slate-700 tracking-wide"
            )

        with ui.row().classes("items-center gap-2"):
            if view_mode["current"] == "form":
                ui.button(
                    icon="unfold_more",
                    on_click=lambda: [
                        e.set_value(True) for e in expansions_ref
                    ],
                ).props("flat dense round size=sm").classes(
                    "text-slate-400 hover:text-indigo-500"
                ).tooltip("Expand all")

                ui.button(
                    icon="unfold_less",
                    on_click=lambda: [
                        e.set_value(False) for e in expansions_ref
                    ],
                ).props("flat dense round size=sm").classes(
                    "text-slate-400 hover:text-indigo-500"
                ).tooltip("Collapse all")

                ui.select(
                    {n: f"{n} cols" for n in range(1, 10)},
                    value=form_cols["n"],
                ).props(
                    "dense outlined options-dense"
                ).classes("w-24").on_value_change(
                    lambda e: _set_cols(e.value, form_cols, editor_area)
                )

            with ui.row().classes(
                "items-center gap-1 bg-slate-100 p-1"
            ):
                _toggle_btn(
                    "Form", "view_list",
                    view_mode["current"] == "form",
                    lambda: _switch_view(
                        "form", state, yaml_holder, view_mode, editor_area
                    ),
                )
                _toggle_btn(
                    "YAML", "code",
                    view_mode["current"] == "yaml",
                    lambda: _switch_view(
                        "yaml", state, yaml_holder, view_mode, editor_area
                    ),
                )


# ═══════════════════════════════════════════════════════════
#  Settings panel (right column)
# ═══════════════════════════════════════════════════════════


def _settings_panel(state, view_mode, yaml_holder, task_generator, task_manager):
    from pyruns.utils.settings import get as _get_ws_setting

    with ui.card().classes(
        "w-full p-3 shadow-sm bg-white border border-slate-100"
    ):
        ui.label("Generation Settings").classes(
            "text-sm font-bold mb-2 text-slate-800"
        )

        ui.input(
            value=state["task_name_input"],
            placeholder="e.g. baseline-run",
            label="Task Name (= Folder Name)",
        ).props(INPUT_PROPS).classes("w-full mb-1").on_value_change(
            lambda e: state.update({"task_name_input": e.value})
        )

        use_ts_chk = ui.checkbox(
            "名称为空时自动使用时间戳命名",
            value=bool(_get_ws_setting("generator_auto_timestamp", True)),
        ).props("dense color=indigo").classes("text-xs text-slate-500 mb-3")

        _batch_syntax_hint()
        ui.separator().classes("mb-2")

        def handle_generate() -> None:
            if view_mode["current"] == "yaml":
                if not _sync_yaml_to_config(yaml_holder["text"], state):
                    return

            if not state.get("config_data"):
                ui.notify("Load a config first!", type="warning")
                return

            prefix = state["task_name_input"].strip()
            if not prefix:
                if use_ts_chk.value:
                    prefix = f"task_{get_now_str()}"
                else:
                    ui.notify(
                        "请输入任务名称，或勾选自动时间戳命名",
                        type="warning", icon="warning",
                    )
                    return

            from pyruns.utils.task_utils import validate_task_name
            err = validate_task_name(prefix)
            if err:
                ui.notify(
                    f"Invalid task name: {err}",
                    type="negative", icon="error",
                )
                return

            try:
                configs = generate_batch_configs(state["config_data"])
            except ValueError as e:
                logger.warning("Batch config error: %s", e)
                ui.notify(str(e), type="negative", icon="error")
                return

            if len(configs) == 1:
                tasks = task_generator.create_tasks(configs, prefix)
                task_manager.add_tasks(tasks)
                state["_manager_dirty"] = True
                logger.info("Generated %d task(s) with prefix '%s'", len(configs), prefix)
                ui.notify(
                    f"Generated {len(configs)} task",
                    type="positive", icon="add_circle",
                )
            else:
                show_batch_confirm(
                    configs, prefix, state["config_data"],
                    task_generator, task_manager, state,
                )

        ui.button(
            "GENERATE", on_click=handle_generate,
        ).props("unelevated icon=rocket_launch").classes(
            f"w-full bg-indigo-600 text-white font-bold tracking-wide "
            f"py-2 hover:bg-indigo-700 shadow-md hover:shadow-lg "
            f"{BTN_CLASS} text-sm"
        )

        ui.label(
            "无 | 语法 → 直接生成  ·  有 | 语法 → 确认后批量生成"
        ).classes("text-[10px] text-slate-400 mt-3 text-center w-full")


# ═══════════════════════════════════════════════════════════
#  Small helpers
# ═══════════════════════════════════════════════════════════


def _set_cols(n: int, form_cols: dict, editor_area) -> None:
    form_cols["n"] = n
    editor_area.refresh()


def _toggle_btn(label: str, icon: str, active: bool, on_click) -> None:
    if active:
        ui.button(label, icon=icon, on_click=on_click).props(
            "unelevated no-caps size=md"
        ).classes(
            "bg-indigo-600 text-white font-bold px-5 py-1.5 "
            "shadow-md text-sm tracking-wide"
        )
    else:
        ui.button(label, icon=icon, on_click=on_click).props(
            "flat no-caps size=md"
        ).classes(
            "text-slate-500 font-medium px-5 py-1.5 text-sm "
            "hover:text-indigo-600 hover:bg-indigo-50/50"
        )


def _switch_view(target, state, yaml_holder, view_mode, editor_area) -> None:
    if view_mode["current"] == target:
        return
    if target == "yaml":
        yaml_holder["text"] = _dict_to_yaml(state.get("config_data", {}))
    else:
        try:
            parsed = yaml.safe_load(yaml_holder["text"])
            if isinstance(parsed, dict):
                state["config_data"] = parsed
        except yaml.YAMLError:
            pass
    view_mode["current"] = target
    editor_area.refresh()


def _dict_to_yaml(data: Dict[str, Any]) -> str:
    if not data:
        return ""
    clean = {k: v for k, v in data.items() if not k.startswith("_meta")}
    return yaml.dump(
        clean, default_flow_style=False,
        allow_unicode=True, sort_keys=False,
    )


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
                ui.notify(
                    "YAML must be a mapping (key: value)",
                    type="negative", icon="error",
                )
            return False
        state["config_data"] = parsed
        return True
    except yaml.YAMLError as e:
        if not quiet:
            msg = str(e)[:120]
            ui.notify(
                f"YAML syntax error:\n{msg}",
                type="negative", icon="error", multi_line=True,
            )
        return False


def _render_yaml_view(yaml_holder: dict) -> None:
    cm = ui.codemirror(
        value=yaml_holder["text"],
        language="YAML", theme="vscodeDark", line_wrapping=True,
    ).classes("w-full overflow-hidden").style(
        "height: calc(100vh - 260px); min-height: 400px;"
    )
    cm.on_value_change(lambda e: yaml_holder.update({"text": e.value}))


def _empty_editor_placeholder() -> None:
    with ui.column().classes("w-full items-center justify-center py-20"):
        ui.icon("description", size="48px").classes("text-slate-200")
        ui.label("No config loaded").classes("text-slate-400 italic mt-2")


def _batch_syntax_hint() -> None:
    with ui.column().classes(
        "w-full gap-1 mb-3 px-2 py-1.5 "
        "bg-slate-50 border border-slate-100"
    ):
        with ui.row().classes("items-center gap-1"):
            ui.icon("info_outline", size="13px").classes("text-indigo-400")
            ui.label("批量语法").classes(
                "text-[10px] font-bold text-slate-600"
            )
        ui.html(
            '<span style="font-family:monospace;font-size:10px;color:#475569;">'
            '<b style="color:#6366f1;">Product</b>: '
            '<code style="background:#e0e7ff;padding:0 3px;border-radius:2px;">'
            "lr: 0.001 | 0.01 | 0.1</code><br>"
            '<b style="color:#8b5cf6;">Zip</b>: &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;'
            '<code style="background:#ede9fe;padding:0 3px;border-radius:2px;">'
            "seed: (1 | 2 | 3)</code><br>"
            '<span style="color:#94a3b8;">Total = ∏(product) × zip_len</span>'
            "</span>",
            sanitize=False,
        )
