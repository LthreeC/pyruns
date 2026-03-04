"""
Task Detail Dialog – displays task info, config, logs, notes, env vars.
"""
import os
import json
from nicegui import ui
from typing import Dict, Any

from pyruns._config import CONFIG_FILENAME
from pyruns.utils.info_io import load_task_info, save_task_info
from pyruns.utils.info_io import get_log_options, resolve_log_path
from pyruns.ui.theme import (
    STATUS_ICONS, DIALOG_BACKDROP, DIALOG_HEADER_DARK, TOOLBAR_DARK, TOOLBAR_LIGHT,
    BTN_PRIMARY, TAB_PANEL_FULL, LABEL_BOLD_TRACKING, INPUT_BORDERLESS_PROPS, INPUT_OUTLINED_CLASSES, TEXTAREA_FULL_BORDERLESS,
    DIALOG_WIDTH_LARGE, DIALOG_TITLE_CLASSES, LOADING_OVERLAY_CLASSES, LOADING_TEXT_CLASSES,
    TABS_PROPS, TABS_CLASSES, TAB_PANELS_CLASSES, TAB_CONTENT_CLASSES, EMPTY_LABEL_CLASSES, ICON_BTN_HOVER_OPACITY
)
from pyruns.ui.widgets import readonly_code_viewer, status_badge
from pyruns.ui.components.env_editor import env_var_editor

from pyruns.utils import get_logger
logger = get_logger(__name__)

# ── Drag JS for movable dialog ──
_DRAG_JS = """(e) => {
    if (e.target.closest('button') || e.target.closest('.q-tab') || e.target.closest('input')) return;
    const card = e.currentTarget.closest('.q-card');
    if (!card) return;
    e.preventDefault();
    const startX = e.clientX, startY = e.clientY;
    const ox = parseFloat(card.dataset.dragX || 0);
    const oy = parseFloat(card.dataset.dragY || 0);
    const onMove = (ev) => {
        const dx = ev.clientX - startX + ox;
        const dy = ev.clientY - startY + oy;
        card.style.transform = 'translate(' + dx + 'px,' + dy + 'px)';
        card.dataset.dragX = dx;
        card.dataset.dragY = dy;
    };
    const onUp = () => {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
}"""


def build_task_dialog(selected: dict, state: Dict[str, Any], task_manager):
    """
    Build the task detail dialog (once, at page init).
    Stores the dialog and open function into `selected["_dialog"]` and `selected["_open_fn"]`.
    """
    selected["state"] = state
    with ui.dialog().props("no-backdrop-dismiss") as task_dialog:
        with ui.card().classes(
            f"task-detail-card {DIALOG_WIDTH_LARGE} {DIALOG_BACKDROP}"
        ).style("height: 80vh; max-height: 80vh;"):

            @ui.refreshable
            def dialog_body():
                t = selected.get("task")
                if not t:
                    ui.label("No task selected.").classes(EMPTY_LABEL_CLASSES)
                    return

                is_loading = selected.get("loading", False)
                info_obj = selected.get("info_obj", {})
                cfg_text = selected.get("cfg_text", "")
                log_options = selected.get("log_options", {})
                log_content = selected.get("log_content", "")
                status = t.get("status", "pending")
                icon_name = STATUS_ICONS.get(status, "help")

                # ── Dialog header (draggable) ──
                header = ui.row().classes(
                    f"{DIALOG_HEADER_DARK} cursor-move select-none"
                )
                header.on("mousedown", js_handler=_DRAG_JS)

                with header:
                    with ui.row().classes("items-center gap-3 flex-1 min-w-0 flex-nowrap"):
                        ui.icon(icon_name, size="22px", color="white")
                        # Add truncate and tooltip to title
                        ui.label(t["name"]).classes(DIALOG_TITLE_CLASSES).tooltip(t["name"])
                        ui.icon("drag_indicator", size="16px").classes("text-white/30 ml-1")
                    with ui.row().classes("items-center gap-3"):
                        status_badge(status, size="md")
                        ui.button(icon="close", on_click=task_dialog.close).props(
                            "flat round dense color=white"
                        ).classes(ICON_BTN_HOVER_OPACITY)


                # ── Tabs ──
                tab_value = selected.get("tab", "task_info")


                with ui.tabs(value=tab_value).props(TABS_PROPS).classes(
                    TABS_CLASSES
                ) as tabs:
                    ui.tab("task_info", label="Task Info", icon="info").classes("px-4")
                    ui.tab("config", label="Config", icon="tune").classes("px-4")
                    ui.tab("notes", label="Notes", icon="edit_note").classes("px-4")
                    ui.tab("env", label="Env Vars", icon="vpn_key").classes("px-4")

                with ui.tab_panels(tabs, value=tab_value).classes(
                    TAB_PANELS_CLASSES
                ).style("padding: 0; min-height: 0;"):

                    if is_loading:
                        with ui.column().classes(LOADING_OVERLAY_CLASSES):
                            ui.spinner('dots', size='3em', color='indigo')
                            ui.label("Loading task details...").classes(LOADING_TEXT_CLASSES)

                    _build_tab_task_info(t, info_obj, selected)
                    _build_tab_config(t, cfg_text)
                    _build_tab_notes(t, info_obj)
                    _build_tab_env_vars(t, info_obj)

            dialog_body()

    async def open_task_dialog(task, tab="task_info"):
        selected["task"] = task
        selected["tab"] = tab
        selected["loading"] = True
        
        # Reset data for instantaneous clean popup
        selected["info_obj"] = {}
        selected["cfg_text"] = ""
        
        task_dialog.open()
        ui.run_javascript("""
            const card = document.querySelector('.task-detail-card');
            if (card) {
                card.style.transform = '';
                card.dataset.dragX = 0;
                card.dataset.dragY = 0;
            }
        """)

        import asyncio
        from nicegui import run
        
        # 1. Let browser paint the dialog instantly
        await asyncio.sleep(0.01)
        
        # 2. Render the skeleton in the dialog body
        dialog_body.refresh()
        
        # 3. Give UI a tick to draw the skeleton
        await asyncio.sleep(0.01)

        def fetch_data():
            info = load_task_info(task["dir"])
            
            cfg_path = os.path.join(task["dir"], CONFIG_FILENAME)
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = f.read().strip() or "# Empty config"
            except Exception:
                cfg = "# No config.yaml"
                
            return info, cfg

        try:
            info, cfg = await run.io_bound(fetch_data)
            selected["info_obj"] = info
            selected["cfg_text"] = cfg
        except Exception as e:
            logger.error(f"Error async loading task dialog data: {e}")
            
        selected["loading"] = False
        dialog_body.refresh()

    selected["_dialog"] = task_dialog
    selected["_open_fn"] = open_task_dialog


# ═══════════════════════════════════════════════════════════════
#  Individual tab builders
# ═══════════════════════════════════════════════════════════════

def _build_tab_task_info(t, info_obj, selected):
    """Task Info tab — readonly JSON viewer and rename control."""
    with ui.tab_panel("task_info").classes(TAB_PANEL_FULL):
        # Rename control
        with ui.row().classes(TOOLBAR_LIGHT):
            with ui.row().classes("items-center gap-2 flex-grow"):
                ui.icon("info", size="18px").classes("text-indigo-500")
                ui.label("Task Name").classes(LABEL_BOLD_TRACKING)
                name_input = ui.input(value=t.get("name", "")).props(INPUT_BORDERLESS_PROPS).classes(INPUT_OUTLINED_CLASSES)
            
            def save_name():
                new_name = name_input.value.strip()
                if not new_name:
                    ui.notify("Name cannot be empty", type="negative")
                    return
                info = load_task_info(t["dir"])
                info["name"] = new_name
                save_task_info(t["dir"], info)
                t["name"] = new_name
                # Update info_obj and refresh to reflect change in JSON view
                if info_obj:
                    info_obj["name"] = new_name
                ui.notify(f"Task renamed to {new_name}", type="positive", icon="check")
                # Trigger a broader refresh via the background refresh mechanism
                if "state" in selected:
                    selected["state"]["_manager_dirty"] = True
                else:
                    # Fallback if state wasn't stored (we should store it in build_task_dialog)
                    pass

            from pyruns.ui.theme import BTN_PRIMARY
            ui.button("Rename", icon="save", on_click=save_name).props("unelevated dense size=sm").classes(f"{BTN_PRIMARY} px-4")

        with ui.column().classes(TAB_CONTENT_CLASSES):
            info_text = (
                json.dumps(info_obj, indent=2, ensure_ascii=False)
                if info_obj else "No task_info.json"
            )
            readonly_code_viewer(info_text, mode="json")


def _build_tab_config(t, cfg_text):
    """Config tab — readonly YAML viewer."""
    with ui.tab_panel("config"):
        readonly_code_viewer(cfg_text, mode="yaml")


def _build_tab_notes(t, info_obj):
    """Notes tab — editable textarea with save button."""
    # 1. Remove padding (p-0) and gap (gap-0) from tab_panel
    # Use flex-col for vertical layout, h-full to fill height
    with ui.tab_panel("notes").classes(TAB_PANEL_FULL):
        
        notes_val = info_obj.get("notes", "") if isinstance(info_obj, dict) else ""
        # Use dict to store reference so callback can access latest value
        notes_holder = {"text": notes_val} 

        # ── Toolbar ──
        # Keep original logic, border-b retains a slight divider feel
        with ui.row().classes(
            TOOLBAR_LIGHT
        ):
            with ui.row().classes("items-center gap-2"):
                ui.icon("edit_note", size="18px").classes("text-indigo-500")
                ui.label("Task Notes & Description").classes(LABEL_BOLD_TRACKING)

            def save_notes():
                info = load_task_info(t["dir"])
                info["notes"] = notes_holder["text"]
                save_task_info(t["dir"], info)
                ui.notify("Notes saved", type="positive", icon="check")

            from pyruns.ui.theme import BTN_PRIMARY
            ui.button("Save", icon="save", on_click=save_notes).props(
                "unelevated dense no-caps size=sm"
            ).classes(f"{BTN_PRIMARY} px-4")

        # ── Textarea ──
        # 2. Remove padding (p-0) from this container, set background to white
        with ui.column().classes(TAB_CONTENT_CLASSES):
            notes_input = ui.textarea(
                value=notes_val,
                placeholder="Record experiment results, parameter notes, observations...",
            ).props(
                # 3. Key modifications:
                # borderless: remove default input border
                # full-width: fill width
                # no-resize: disable bottom-right drag handle
                "borderless full-width no-resize"
            ).classes(TEXTAREA_FULL_BORDERLESS).style(
                "line-height: 1.6; height: 100%;"
            )
            
            # Bind data update
            notes_input.on_value_change(
                lambda e: notes_holder.update({"text": e.value})
            )


def _build_tab_env_vars(t, info_obj):
    """Env Vars tab — key/value editor."""
    with ui.tab_panel("env"):
        env = info_obj.get("env", {}) if isinstance(info_obj, dict) else {}
        rows = [{"key": k, "val": v} for k, v in env.items()]

        def on_save_env(new_env):
            info = load_task_info(t["dir"])
            info["env"] = new_env
            save_task_info(t["dir"], info)
            t["env"] = new_env
            ui.notify("Env vars saved", type="positive", icon="check")

        env_var_editor(rows, on_save=on_save_env)

