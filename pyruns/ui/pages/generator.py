import os
from nicegui import ui
from typing import Dict, Any

from pyruns.utils.config_utils import load_yaml, generate_batch_configs, list_template_files
from pyruns.ui.components.param_editor import recursive_param_editor
from pyruns.ui.theme import INPUT_PROPS, BTN_CLASS
from pyruns.ui.widgets import dir_picker
from pyruns.utils import get_logger

logger = get_logger(__name__)

def render_generator_page(state: Dict[str, Any], task_generator, task_manager) -> None:
    """Page 1: Task Generator (Side-by-Side)"""

    # 1. Config Loading Bar (Full Width)
    with ui.row().classes("w-full items-center gap-4 mb-6 bg-white p-4 rounded-xl shadow-sm border border-slate-100"):
        tasks_dir_input = dir_picker(
            value=state["tasks_dir"],
            label="Tasks Root",
            on_change=lambda path: (state.update({"tasks_dir": path}), refresh_files()),
            input_classes="flex-grow",
        )

        file_select = ui.select([], label="Template").props(INPUT_PROPS).classes("w-64")

        def on_file_select_change() -> None:
            if not file_select.value:
                logger.warning("[Generator] on_file_select_change: No file selected")
                return
            
            # file_select.value 现在直接就是文件路径（因为字典反转了）
            val = file_select.value
            logger.info(f"[Generator] Selected file path: {val}")
            
            # SUPPORT FOR PARENT DIR CONFIG
            if val.startswith(".."):
                 # Handle relative path from tasks_dir
                 path = os.path.abspath(os.path.join(state["tasks_dir"], val))
            else:
                 path = os.path.join(state["tasks_dir"], val)

            logger.info(f"[Generator] Absolute path: {path}")
            if os.path.exists(path):
                config_data = load_yaml(path)
                logger.info(f"[Generator] Loaded config with {len(config_data)} keys: {list(config_data.keys())}")
                state["config_data"] = config_data
                state["config_path"] = path
                # 只有在 editor_area 已定义时才刷新（避免初始化时的作用域错误）
                try:
                    editor_area.refresh()
                except NameError:
                    # editor_area 尚未定义（初始化阶段），跳过刷新
                    pass
            else:
                logger.error(f"[Generator] Config file not found: {path}")

        file_select.on_value_change(on_file_select_change)

        def refresh_files() -> None:
            state["tasks_dir"] = tasks_dir_input.value
            options = list_template_files(state["tasks_dir"])
            file_select.options = options
            if options and not file_select.value:
                file_select.value = list(options.keys())[0]  # key 现在是路径
            if file_select.value:
                on_file_select_change()

        ui.button(icon="refresh", on_click=refresh_files).props("flat round color=slate")

        if not file_select.options:
            refresh_files()

    # 2. Main Content (Side-by-Side)
    with ui.row().classes("w-full gap-6 flex-nowrap items-start"):
        # LEFT: Parameter Editor (Flex Grow)
        with ui.column().classes("flex-grow min-w-0"):
            @ui.refreshable
            def editor_area() -> None:
                config_data = state.get("config_data")
                if not config_data and file_select.value:
                    on_file_select_change()
                    config_data = state.get("config_data")

                if config_data:
                    with ui.column().classes("w-full gap-3"):
                        recursive_param_editor(ui.column().classes("w-full gap-3"), config_data, state, task_manager)
                else:
                    ui.label("No config loaded.").classes("text-slate-400 italic")

            editor_area()

        # RIGHT: Settings & Generate Button (Sticky, Fixed Width)
        with ui.column().classes("w-96 flex-none sticky top-4"):
            with ui.card().classes("w-full p-6 shadow-md rounded-2xl bg-white border border-slate-100"):
                ui.label("Generation Settings").classes("text-lg font-bold mb-4 text-slate-800")

                ui.input(
                    value=state["task_name_input"],
                    placeholder="e.g. baseline-run",
                    label="Task Name (= Folder Name)"
                ).props(INPUT_PROPS).classes("w-full mb-8").on_value_change(lambda e: state.update({"task_name_input": e.value}))

                ui.separator().classes("mb-6")

                def handle_generate(is_batch: bool = False) -> None:
                    if not state.get("config_data"):
                        ui.notify("Load a config first!", type="warning")
                        return

                    prefix = state["task_name_input"]

                    # ── 校验任务名是否可作为文件夹名 ──
                    from pyruns.utils.task_utils import validate_task_name
                    err = validate_task_name(prefix)
                    if err:
                        ui.notify(f"Invalid task name: {err}", type="negative", icon="error")
                        return

                    configs = generate_batch_configs(state["config_data"]) if is_batch else [state["config_data"]]
                    use_ts = state["use_timestamp_name"]

                    tasks = task_generator.create_tasks(configs, prefix, use_ts)
                    task_manager.add_tasks(tasks)

                    ui.notify(f"Generated {len(configs)} tasks (Pending)", type="positive", icon="add_circle")

                ui.button("GENERATE TASK", on_click=lambda: handle_generate(False)).props("unelevated icon=add_circle").classes(
                    f"w-full bg-indigo-600 text-white font-bold tracking-wide rounded-lg py-4 hover:bg-indigo-700 shadow-lg hover:shadow-xl {BTN_CLASS} mb-3 text-lg"
                )

                ui.button("Batch Generate (Permutations)", on_click=lambda: handle_generate(True)).props("outline icon=grid_view").classes(
                    f"w-full text-indigo-600 border-indigo-200 hover:bg-indigo-50 rounded-lg py-2 {BTN_CLASS}"
                )

                ui.label("Tasks will be created in PENDING state.").classes("text-[10px] text-slate-400 mt-4 text-center w-full")
