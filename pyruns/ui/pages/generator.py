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
from pyruns.utils import get_logger, get_now_str, client_connected
from pyruns.ui.components.param_editor import recursive_param_editor
from pyruns.ui.components.batch_dialog import show_batch_confirm
from pyruns.ui.theme import (
    INPUT_PROPS, ICON_BTN_MUTED_CLASSES,
    GENERATOR_HEADER_CLASSES, GENERATOR_WORKSPACE_CLASSES,
    GENERATOR_LEFT_COL_CLASSES, GENERATOR_RIGHT_COL_CLASSES,
    GENERATOR_TOOLBAR_CLASSES, GENERATOR_SETTINGS_CARD_CLASSES,
    GENERATOR_TEMPLATE_SELECT_CLASSES, GENERATOR_VIEW_TOGGLE_WRAP_CLASSES,
    GENERATOR_FORM_SCROLL_CLASSES, GENERATOR_FORM_ROOT_CLASSES,
    GENERATOR_WARNING_ROW_CLASSES, GENERATOR_WARNING_ICON_CLASSES,
    GENERATOR_WARNING_TEXT_CLASSES, GENERATOR_YAML_EDITOR_CLASSES,
    GENERATOR_ARGS_CONTAINER_CLASSES, GENERATOR_ARGS_EXPANSION_CLASSES,
    GENERATOR_ARGS_RUNSCRIPT_INPUT_CLASSES, GENERATOR_ARGS_TEXTAREA_CLASSES,
    GENERATOR_GENERATE_BTN_CLASSES,
    GENERATOR_ACTIVE_TOGGLE_BTN_CLASSES, GENERATOR_INACTIVE_TOGGLE_BTN_CLASSES,
    EMPTY_STATE_COL_CLASSES, EMPTY_STATE_ICON_SIZE,
    EMPTY_STATE_ICON_CLASSES, EMPTY_STATE_TEXT_CLASSES,
    BATCH_HINT_BOX_CLASSES, BATCH_HINT_TITLE_CLASSES,
    BATCH_HINT_FOOTER_CLASSES, REFRESH_ICON_BTN_PROPS,
    COMPACT_SELECT_CLASSES, TEXT_HEADING_SM, TEXT_MUTED_XS,
    ROW_CENTER_GAP_1, ROW_CENTER_GAP_2, COL_FULL
)
from pyruns.ui.widgets import dir_picker, _ensure_css
from pyruns._config import (
    DEFAULT_GENERATOR_MODE, DEFAULT_GENERATOR_FORM_COLUMNS, DEFAULT_GENERATOR_AUTO_TIMESTAMP,
)

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
    script_info = load_script_info(str(state.get("run_root", "")).replace("\\", "/"))
    script_path = script_info.get("script_path", "")
    script_mode = "unknown"
    if script_path and os.path.exists(script_path):
        try:
            from pyruns.utils.parse_utils import detect_config_source_fast
            script_mode, _ = detect_config_source_fast(script_path)
        except Exception:
            script_mode = "unknown"

    saved_mode = str(_get_setting("generate_mode", DEFAULT_GENERATOR_MODE) or DEFAULT_GENERATOR_MODE).lower()
    initial_mode = saved_mode if saved_mode in ("form", "yaml", "args") else "form"

    view_mode = {"current": initial_mode}
    form_cols = {"n": int(_get_setting("generator_form_columns", DEFAULT_GENERATOR_FORM_COLUMNS))}
    yaml_holder = {"text": ""}
    args_holder = {"text": ""}
    run_script_holder = {"text": ""}
    expansions_ref: list = []
    config_default_exists = os.path.exists(
        os.path.join(str(state.get("run_root", "")).replace("\\", "/"), "config_default.yaml")
    )

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
        ).props(INPUT_PROPS).classes(GENERATOR_TEMPLATE_SELECT_CLASSES)

        # Read-only indicator for config_default.yaml
        with ui.row().classes(ROW_CENTER_GAP_1):
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
                if _is_args_only_config(config_data):
                    args_holder["text"] = str(config_data.get("args", "") or "")
                    run_script_holder["text"] = str(config_data.get("run_script", "") or _default_run_script(script_path))
                    view_mode["current"] = "args"
                elif view_mode["current"] == "args":
                    view_mode["current"] = "form"
                logger.debug("Loaded template: %s", val)
                try:
                    if client_connected():
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
        ).props(REFRESH_ICON_BTN_PROPS)

        if not file_select.options:
            refresh_files()

        if script_mode in ("hydra", "unknown") and not config_default_exists:
            ui.notify(
                "No config_default.yaml detected for this script; Args mode is the default.",
                type="info",
                icon="info",
                timeout=3500,
            )

        # ── Listen for run_root changes from other pages ──
        from pyruns.utils.events import event_sys

        def _on_root_changed(new_path):
            if not client_connected():
                return
            run_root_input.value = new_path
            refresh_files()
            try:
                editor_area.refresh()
            except NameError:
                pass

        event_sys.on("on_run_root_change", _on_root_changed)

        client = ui.context.client
        client.on_disconnect(lambda: event_sys.off("on_run_root_change", _on_root_changed))

    # ══════════════════════════════════════════════════════════
    #  Row 2 — Main Workspace (Side-by-side)
    # ══════════════════════════════════════════════════════════
    # Use h-[calc...] to ensure maximum space utilization on large screens
    with ui.row().classes(GENERATOR_WORKSPACE_CLASSES):

        # ── LEFT: Parameter editor (Flexible Width) ──
        with ui.column().classes(GENERATOR_LEFT_COL_CLASSES):

            @ui.refreshable
            def editor_area() -> None:
                if not client_connected():
                    return
                config_data = state.get("config_data")
                if not config_data and file_select.value:
                    on_file_select_change()
                    config_data = state.get("config_data")

                _editor_toolbar(
                    view_mode, form_cols, expansions_ref,
                    state, yaml_holder, args_holder, run_script_holder, editor_area,
                )

                if not config_data and view_mode["current"] in ("form", "yaml"):
                    _empty_editor_placeholder(
                        mode=view_mode["current"],
                        need_default_hint=(not config_default_exists),
                    )
                    return

                if view_mode["current"] in ("form", "yaml") and script_mode in ("hydra", "unknown"):
                    with ui.row().classes(
                        GENERATOR_WARNING_ROW_CLASSES
                    ):
                        ui.icon("warning", size="16px").classes(GENERATOR_WARNING_ICON_CLASSES)
                        msg = (
                            "This script looks like Hydra/unknown parser. "
                            "Config mode may not match runtime behavior. Prefer Args mode."
                        )
                        if not config_default_exists:
                            msg += " No config_default.yaml found; defaulting to Args mode is recommended."
                        ui.label(msg).classes(GENERATOR_WARNING_TEXT_CLASSES)

                if view_mode["current"] == "form":
                    expansions_ref.clear()
                    with ui.column().classes(GENERATOR_FORM_SCROLL_CLASSES):
                        recursive_param_editor(
                            ui.column().classes(GENERATOR_FORM_ROOT_CLASSES),
                            config_data, state, task_manager,
                            columns=form_cols["n"],
                            expansions=expansions_ref,
                            on_pin_toggle=lambda: editor_area.refresh(),
                        )
                elif view_mode["current"] == "yaml":
                    _render_yaml_view(yaml_holder)
                else:
                    _render_args_view(args_holder, run_script_holder, script_path)

            editor_area()
            _editor_area_ref[0] = editor_area

        # ── RIGHT: Settings & generate button ──
        with ui.column().classes(GENERATOR_RIGHT_COL_CLASSES):
            _settings_panel(
                state, view_mode, yaml_holder, args_holder,
                run_script_holder, script_path,
                task_generator, task_manager,
                refresh_files
            )


# ═══════════════════════════════════════════════════════════
#  Editor toolbar (view toggle + column selector)
# ═══════════════════════════════════════════════════════════


def _editor_toolbar(
    view_mode, form_cols, expansions_ref,
    state, yaml_holder, args_holder, run_script_holder, editor_area,
) -> None:
    with ui.row().classes(GENERATOR_TOOLBAR_CLASSES):
        with ui.row().classes(ROW_CENTER_GAP_2):
            ui.icon("tune", size="20px").classes("text-indigo-500")
            ui.label("Parameters").classes(
                TEXT_HEADING_SM
            )

        with ui.row().classes(ROW_CENTER_GAP_2):
            if view_mode["current"] == "form":
                ui.button(
                    icon="unfold_more",
                    on_click=lambda: [
                        e.set_value(True) for e in expansions_ref
                    ],
                ).props("flat dense round size=sm").classes(
                    ICON_BTN_MUTED_CLASSES
                ).tooltip("Expand all")

                ui.button(
                    icon="unfold_less",
                    on_click=lambda: [
                        e.set_value(False) for e in expansions_ref
                    ],
                ).props("flat dense round size=sm").classes(
                    ICON_BTN_MUTED_CLASSES
                ).tooltip("Collapse all")

                ui.select(
                    {n: f"{n} cols" for n in range(1, 10)},
                    value=form_cols["n"],
                ).props(
                    "dense outlined options-dense"
                ).classes(COMPACT_SELECT_CLASSES).on_value_change(
                    lambda e: _set_cols(e.value, form_cols, editor_area)
                )

            with ui.row().classes(GENERATOR_VIEW_TOGGLE_WRAP_CLASSES):
                _toggle_btn(
                    "Form", "view_list",
                    view_mode["current"] == "form",
                    lambda: _switch_view(
                        "form", state, yaml_holder, args_holder, run_script_holder, view_mode, editor_area
                    ),
                )
                _toggle_btn(
                    "YAML", "code",
                    view_mode["current"] == "yaml",
                    lambda: _switch_view(
                        "yaml", state, yaml_holder, args_holder, run_script_holder, view_mode, editor_area
                    ),
                )
                _toggle_btn(
                    "Args", "terminal",
                    view_mode["current"] == "args",
                    lambda: _switch_view(
                        "args", state, yaml_holder, args_holder, run_script_holder, view_mode, editor_area
                    ),
                )


# ═══════════════════════════════════════════════════════════
#  Settings panel (right column)
# ═══════════════════════════════════════════════════════════


def _settings_panel(
    state, view_mode, yaml_holder, args_holder, run_script_holder, script_path,
    task_generator, task_manager, refresh_files
):
    from pyruns.utils.settings import get as _get_ws_setting, save_setting

    with ui.card().classes(GENERATOR_SETTINGS_CARD_CLASSES):
        ui.label("Generation Settings").classes(TEXT_HEADING_SM + " mb-2 text-slate-800")

        ui.input(
            value=state["task_name_input"],
            placeholder="task",
            label="Task Name (= Folder Name)",
        ).props(INPUT_PROPS).classes(COL_FULL + " mb-1").on_value_change(
            lambda e: state.update({"task_name_input": e.value})
        )

        use_ts_chk = ui.checkbox(
            "Append timestamp suffix to task name",
            value=bool(_get_ws_setting("generator_auto_timestamp", DEFAULT_GENERATOR_AUTO_TIMESTAMP)),
        ).props("dense color=indigo").classes(TEXT_MUTED_XS + " mb-3")
        use_ts_chk.on_value_change(lambda e: save_setting("generator_auto_timestamp", bool(e.value)))

        _batch_syntax_hint()
        ui.separator().classes("mb-2")

        async def handle_generate():
            if view_mode["current"] == "yaml":
                if not _sync_yaml_to_config(yaml_holder["text"], state):
                    return

            run_mode = "args" if view_mode["current"] == "args" else "config"
            if run_mode == "args":
                target_config = {
                    "run_script": str(run_script_holder.get("text", "") or _default_run_script(script_path)),
                    "args": str(args_holder.get("text", "") or ""),
                }
            else:
                target_config = state.get("config_data") or {}
                if not state.get("config_data"):
                    ui.notify("No config loaded, running with empty configuration.", type="info")

            prefix = state["task_name_input"].strip()
            if not prefix:
                prefix = "task"
                
            if use_ts_chk.value:
                prefix = f"{prefix}_{get_now_str()}"

            from pyruns.utils.info_io import validate_task_name
            err = validate_task_name(prefix, task_manager.tasks_dir)
            if err:
                ui.notify(
                    f"Invalid task name: {err}",
                    type="negative", icon="error",
                )
                return

            try:
                configs = generate_batch_configs(target_config)
            except ValueError as e:
                logger.warning("Batch config error: %s", e)
                ui.notify(str(e), type="negative", icon="error")
                return

            # --- Type Checking against Original Template ---
            if (
                run_mode == "config"
                and state.get("config_path")
                and os.path.exists(state["config_path"])
                and target_config
            ):
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
                    tasks = await run.io_bound(
                        task_generator.create_tasks, configs, prefix, run_mode
                    )
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
                    configs, prefix, target_config,
                    task_generator, task_manager,
                    run_mode=run_mode,
                    on_success=on_batch_success
                )

        from pyruns.ui.theme import BTN_PRIMARY
        ui.button(
            "GENERATE", on_click=handle_generate,
        ).props("unelevated icon=rocket_launch").classes(
            f"{BTN_PRIMARY} {GENERATOR_GENERATE_BTN_CLASSES}"
        )

        ui.label(
            "No | → Direct Gen  .  Given | → Batch Gen After Confirm"
        ).classes(BATCH_HINT_FOOTER_CLASSES)


# ═══════════════════════════════════════════════════════════
#  Small helpers
# ═══════════════════════════════════════════════════════════


def _set_cols(n: int, form_cols: dict, editor_area) -> None:
    form_cols["n"] = n
    from pyruns.utils.settings import save_setting
    save_setting("generator_form_columns", int(n))
    editor_area.refresh()


def _toggle_btn(label: str, icon: str, active: bool, on_click) -> None:
    if active:
        from pyruns.ui.theme import BTN_PRIMARY
        ui.button(label, icon=icon, on_click=on_click).props(
            "unelevated no-caps size=md"
        ).classes(
            f"{BTN_PRIMARY} {GENERATOR_ACTIVE_TOGGLE_BTN_CLASSES}"
        )
    else:
        ui.button(label, icon=icon, on_click=on_click).props(
            "flat no-caps size=md"
        ).classes(
            GENERATOR_INACTIVE_TOGGLE_BTN_CLASSES
        )


def _switch_view(target, state, yaml_holder, args_holder, run_script_holder, view_mode, editor_area) -> None:
    if view_mode["current"] == target:
        return
    current = view_mode["current"]
    if current == "yaml":
        try:
            parsed = yaml.safe_load(yaml_holder["text"])
            if isinstance(parsed, dict):
                state["config_data"] = parsed
        except yaml.YAMLError:
            pass
    elif current == "args":
        args_holder["text"] = str(args_holder.get("text", "") or "")

    if target == "yaml":
        yaml_holder["text"] = _dict_to_yaml(state.get("config_data", {}))
    elif target == "args":
        cfg = state.get("config_data") or {}
        if _is_args_only_config(cfg):
            args_holder["text"] = str(cfg.get("args", "") or "")
            run_script_holder["text"] = str(cfg.get("run_script", "") or "")
    view_mode["current"] = target
    try:
        from pyruns.utils.settings import save_setting
        save_setting("generate_mode", target)
    except Exception:
        pass
    editor_area.refresh()


def _dict_to_yaml(data: Dict[str, Any]) -> str:
    if not data:
        return ""
    clean = {k: v for k, v in data.items() if not k.startswith("_meta")}
    return yaml.dump(
        clean, default_flow_style=False,
        allow_unicode=True, sort_keys=False,
    )


def _default_run_script(script_path: str) -> str:
    if script_path:
        # Quote path if needed; value is later tokenized via split_cli_args.
        return f'python "{script_path}"'
    return "python"


def _is_args_only_config(data: Dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return False
    keys = [k for k in data.keys() if not str(k).startswith("_meta")]
    return set(keys).issubset({"args", "run_script"})


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
    ).classes(GENERATOR_YAML_EDITOR_CLASSES)
    cm.on_value_change(lambda e: yaml_holder.update({"text": e.value}))


def _render_args_view(args_holder: dict, run_script_holder: dict, script_path: str) -> None:
    with ui.column().classes(GENERATOR_ARGS_CONTAINER_CLASSES):
        with ui.row().classes(ROW_CENTER_GAP_2):
            ui.icon("terminal", size="18px").classes("text-emerald-600")
            ui.label("Args Mode").classes(TEXT_HEADING_SM + " text-slate-800")
            ui.badge("run as: <run_script> <args>").props(
                "color=emerald-1 text-color=emerald-8"
            ).classes("text-[10px]")

        with ui.expansion("Run Script (Advanced)", icon="settings", value=False).classes(
            GENERATOR_ARGS_EXPANSION_CLASSES
        ):
            ui.input(
                value=str(run_script_holder.get("text", "") or _default_run_script(script_path)),
                label="run_script",
                placeholder='python "path/to/train.py"  or  python -m package.module',
            ).props("outlined dense").classes(GENERATOR_ARGS_RUNSCRIPT_INPUT_CLASSES).on_value_change(
                lambda e: run_script_holder.update({"text": e.value})
            )

        ui.textarea(
            value=str(args_holder.get("text", "") or ""),
            placeholder=(
                "args here"
            ),
        ).props(
            "outlined autogrow input-style='font-family:Consolas,Monaco,monospace;line-height:1.5;'"
        ).classes(GENERATOR_ARGS_TEXTAREA_CLASSES).on_value_change(
            lambda e: args_holder.update({"text": e.value})
        )


def _empty_editor_placeholder(mode: str = "form", need_default_hint: bool = False) -> None:
    with ui.column().classes(EMPTY_STATE_COL_CLASSES):
        ui.icon("description", size=EMPTY_STATE_ICON_SIZE).classes(EMPTY_STATE_ICON_CLASSES)
        if mode == "yaml":
            text = "YAML mode is empty. Load or create config_default.yaml first."
        else:
            text = "Form mode is empty. Load or create config_default.yaml first."
        if need_default_hint:
            text += " (config_default.yaml not found)"
        ui.label(text).classes(EMPTY_STATE_TEXT_CLASSES)


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
