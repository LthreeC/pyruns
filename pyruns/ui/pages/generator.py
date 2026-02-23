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

from pyruns.utils.config_utils import load_yaml, list_template_files
from pyruns.utils.info_io import load_script_info, save_script_info
from pyruns.utils.batch_utils import generate_batch_configs
from pyruns.utils import get_logger, get_now_str
from pyruns.ui.components.param_editor import recursive_param_editor
from pyruns.ui.components.batch_dialog import show_batch_confirm
from pyruns.ui.theme import (
    INPUT_PROPS, BTN_CLASS,
    GENERATOR_HEADER_CLASSES, GENERATOR_WORKSPACE_CLASSES,
    GENERATOR_LEFT_COL_CLASSES, GENERATOR_RIGHT_COL_CLASSES,
    GENERATOR_TOOLBAR_CLASSES, GENERATOR_SETTINGS_CARD_CLASSES,
    EMPTY_STATE_COL_CLASSES, EMPTY_STATE_ICON_SIZE,
    EMPTY_STATE_ICON_CLASSES, EMPTY_STATE_TEXT_CLASSES,
    BATCH_HINT_BOX_CLASSES, BATCH_HINT_TITLE_CLASSES,
    BATCH_HINT_FOOTER_CLASSES
)
from pyruns.ui.widgets import dir_picker, _ensure_css

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
    with ui.row().classes(GENERATOR_HEADER_CLASSES):
        curr_run_root = str(state.get("run_root")).replace("\\", "/")

        def _on_dir_change(p):
            state["change_run_root"](p)

        run_root_input = dir_picker(
            value=curr_run_root,
            label="Run Root",
            on_change=_on_dir_change,
            input_classes="flex-grow",
        )

        file_select = ui.select(
            [], label="Template"
        ).props(INPUT_PROPS).classes("w-64")

        # Read-only indicator for config_default.yaml
        with ui.row().classes("items-center gap-1"):
            from pyruns._config import CONFIG_DEFAULT_FILENAME
            tpl_lock = ui.icon("lock", size="16px").classes("text-slate-300")
            tpl_lock.tooltip(
                f"Templates are read-only — edits will not modify original {CONFIG_DEFAULT_FILENAME}"
            )
            tpl_lock.set_visibility(False)

        def on_file_select_change() -> None:
            if not file_select.value:
                return
            val = file_select.value
            curr_run_root = str(state.get("run_root")).replace("\\", "/")
            
            # Save the last used template
            try:
                s_info = load_script_info(curr_run_root)
                s_info["last_used_template"] = val
                save_script_info(curr_run_root, s_info)
            except Exception as e:
                logger.error("Failed to save last_used_template: %s", e)
            
            if os.path.isabs(val):
                path = val
            elif val.startswith(".."):
                path = os.path.abspath(os.path.join(curr_run_root, val))
            else:
                path = os.path.join(curr_run_root, val)

            from pyruns._config import CONFIG_DEFAULT_FILENAME
            tpl_lock.set_visibility(CONFIG_DEFAULT_FILENAME in val)

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

        def refresh_files(new_selection=None) -> None:
            curr_run_root = str(state.get("run_root")).replace("\\", "/")
            options = list_template_files(curr_run_root)
            file_select.set_options(options)
            file_select.update()
            
            # Auto-select the newly generated one if requested
            if new_selection and new_selection in options:
                file_select.value = new_selection
                return
                
            # If nothing selected and default exists, select it
            if not file_select.value and file_select.options:
                target_val = None
                s_info = load_script_info(curr_run_root)
                last_used = s_info.get("last_used_template")
                if last_used and last_used in options:
                    target_val = last_used
                else:
                    target_val = list(options.keys())[0]

                if target_val and target_val != file_select.value:
                    file_select.value = target_val
                elif target_val:
                    on_file_select_change()

        ui.button(
            icon="refresh", on_click=refresh_files
        ).props("flat round dense color=slate")

        if not file_select.options:
            refresh_files()

        # ── Listen for run_root changes from other pages ──
        from pyruns.utils.events import event_sys

        def _on_root_changed(new_path):
            run_root_input.value = new_path
            refresh_files()
            try:
                editor_area.refresh()
            except NameError:
                pass

        event_sys.on("on_run_root_change", _on_root_changed)

    # ══════════════════════════════════════════════════════════
    #  Row 2 — Main Workspace (Side-by-side)
    # ══════════════════════════════════════════════════════════
    # Use h-[calc...] to ensure maximum space utilization on large screens
    with ui.row().classes(GENERATOR_WORKSPACE_CLASSES):

        # ── LEFT: Parameter editor (Flexible Width) ──
        with ui.column().classes(GENERATOR_LEFT_COL_CLASSES):

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
                    with ui.column().classes("w-full overflow-y-auto flex-grow gap-0").style("min-height: 0;"):
                        recursive_param_editor(
                            ui.column().classes("w-full gap-0 p-0 m-0"),
                            config_data, state, task_manager,
                            columns=form_cols["n"],
                            expansions=expansions_ref,
                            on_pin_toggle=lambda: editor_area.refresh(),
                        )
                else:
                    _render_yaml_view(yaml_holder)

            editor_area()
            _editor_area_ref[0] = editor_area

        # ── RIGHT: Settings & generate button ──
        with ui.column().classes(GENERATOR_RIGHT_COL_CLASSES):
            _settings_panel(
                state, view_mode, yaml_holder,
                task_generator, task_manager,
                refresh_files
            )


# ═══════════════════════════════════════════════════════════
#  Editor toolbar (view toggle + column selector)
# ═══════════════════════════════════════════════════════════


def _editor_toolbar(
    view_mode, form_cols, expansions_ref,
    state, yaml_holder, editor_area,
) -> None:
    with ui.row().classes(GENERATOR_TOOLBAR_CLASSES):
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


def _settings_panel(state, view_mode, yaml_holder, task_generator, task_manager, refresh_files):
    from pyruns.utils.settings import get as _get_ws_setting

    with ui.card().classes(GENERATOR_SETTINGS_CARD_CLASSES):
        ui.label("Generation Settings").classes(
            "text-sm font-bold mb-2 text-slate-800"
        )

        ui.input(
            value=state["task_name_input"],
            placeholder="task",
            label="Task Name (= Folder Name)",
        ).props(INPUT_PROPS).classes("w-full mb-1").on_value_change(
            lambda e: state.update({"task_name_input": e.value})
        )

        use_ts_chk = ui.checkbox(
            "Append timestamp suffix to task name",
            value=bool(_get_ws_setting("generator_auto_timestamp", True)),
        ).props("dense color=indigo").classes("text-xs text-slate-500 mb-3")

        _batch_syntax_hint()
        ui.separator().classes("mb-2")

        async def handle_generate():
            if view_mode["current"] == "yaml":
                if not _sync_yaml_to_config(yaml_holder["text"], state):
                    return

            if not state.get("config_data"):
                ui.notify("Load a config first!", type="warning")
                return

            prefix = state["task_name_input"].strip()
            if not prefix:
                prefix = "task"
                
            if use_ts_chk.value:
                prefix = f"{prefix}_{get_now_str()}"

            from pyruns.utils.info_io import validate_task_name
            err = validate_task_name(prefix, task_manager.root_dir)
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

            # --- Type Checking against Original Template ---
            if state.get("config_path") and os.path.exists(state["config_path"]):
                from pyruns.utils.config_utils import load_yaml, validate_config_types_against_template
                try:
                    orig_config = load_yaml(state["config_path"])
                    err_msg = validate_config_types_against_template(orig_config, configs)
                    if err_msg:
                        ui.notify(err_msg, type="negative", icon="error", multi_line=True, timeout=5000)
                        return
                except Exception as e:
                    logger.warning(f"Type check failed to read template: {e}")
            # ---------------------------------------------

            if len(configs) == 1:
                try:
                    from nicegui import run
                    tasks = await run.io_bound(task_generator.create_tasks, configs, prefix)
                    task_manager.add_tasks(tasks)
                    logger.info("Generated %d task(s) with prefix '%s'", len(configs), prefix)
                    ui.notify(
                        f"Generated {len(configs)} task",
                        type="positive", icon="add_circle",
                    )
                    
                    # Compute relative path for selection
                    new_rel = None
                    if tasks:
                        from pyruns._config import TASKS_DIR, CONFIG_FILENAME
                        new_rel = f"{TASKS_DIR}/{tasks[0]['name']}/{CONFIG_FILENAME}"
                    refresh_files(new_selection=new_rel)
                    
                except Exception as e:
                    ui.notify(f"Generation error: {e}", type="negative", icon="error")
            else:
                def on_batch_success(tasks_list=None):
                    new_rel = None
                    if tasks_list:
                        from pyruns._config import TASKS_DIR, CONFIG_FILENAME
                        new_rel = f"{TASKS_DIR}/{tasks_list[0]['name']}/{CONFIG_FILENAME}"
                    refresh_files(new_selection=new_rel)

                show_batch_confirm(
                    configs, prefix, state["config_data"],
                    task_generator, task_manager, state,
                    on_success=on_batch_success
                )

        from pyruns.ui.theme import BTN_PRIMARY
        ui.button(
            "GENERATE", on_click=handle_generate,
        ).props("unelevated icon=rocket_launch").classes(
            f"{BTN_PRIMARY} w-full text-sm font-bold py-2 tracking-wide rounded"
        )

        ui.label(
            "No | → Direct Gen  .  Given | → Batch Gen After Confirm"
        ).classes(BATCH_HINT_FOOTER_CLASSES)


# ═══════════════════════════════════════════════════════════
#  Small helpers
# ═══════════════════════════════════════════════════════════


def _set_cols(n: int, form_cols: dict, editor_area) -> None:
    form_cols["n"] = n
    editor_area.refresh()


def _toggle_btn(label: str, icon: str, active: bool, on_click) -> None:
    if active:
        from pyruns.ui.theme import BTN_PRIMARY
        ui.button(label, icon=icon, on_click=on_click).props(
            "unelevated no-caps size=md"
        ).classes(
            f"{BTN_PRIMARY} px-5 py-1.5 font-bold text-sm tracking-wide rounded"
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
    with ui.column().classes(EMPTY_STATE_COL_CLASSES):
        ui.icon("description", size=EMPTY_STATE_ICON_SIZE).classes(EMPTY_STATE_ICON_CLASSES)
        ui.label("No config loaded").classes(EMPTY_STATE_TEXT_CLASSES)


def _batch_syntax_hint() -> None:
    with ui.column().classes(BATCH_HINT_BOX_CLASSES):
        with ui.row().classes("items-center gap-1"):
            ui.icon("info_outline", size="13px").classes("text-indigo-400")
            ui.label("Batch Syntax").classes(BATCH_HINT_TITLE_CLASSES)
        from pyruns.ui.theme import get_generator_batch_hint_html
        from pyruns._config import BATCH_SEPARATOR, BATCH_ESCAPE
        
        ui.html(
            get_generator_batch_hint_html(BATCH_SEPARATOR, BATCH_ESCAPE),
            sanitize=False,
        )
