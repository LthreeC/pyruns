"""
Task Detail Dialog – displays task info, config, logs, notes, env vars.
"""
import os
import json
from nicegui import ui
from typing import Dict, Any

from pyruns._config import LOG_FILENAME, RERUN_LOG_DIR
from pyruns.utils.config_utils import load_yaml, load_task_info, save_task_info
from pyruns.ui.theme import STATUS_ICONS
from pyruns.ui.widgets import (
    status_badge, readonly_code_viewer, env_var_editor,
)


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


def get_log_options(task_dir: str) -> Dict[str, str]:
    """Return {display_name: file_path} for run.log + rerun logs."""
    options = {}
    run_log = os.path.join(task_dir, LOG_FILENAME)
    if os.path.exists(run_log):
        options["run.log"] = run_log

    rerun_dir = os.path.join(task_dir, RERUN_LOG_DIR)
    if os.path.isdir(rerun_dir):
        rerun_files = sorted(
            [f for f in os.listdir(rerun_dir) if f.startswith("rerun") and f.endswith(".log")],
            key=lambda x: int("".join(filter(str.isdigit, x)) or "0"),
        )
        for f in rerun_files:
            options[f] = os.path.join(rerun_dir, f)

    return options


def build_task_dialog(selected: dict, state: Dict[str, Any], task_manager):
    """
    Build the task detail dialog (once, at page init).
    Stores the dialog and open function into `selected["_dialog"]` and `selected["_open_fn"]`.
    """
    with ui.dialog().props("no-backdrop-dismiss") as task_dialog:
        with ui.card().classes(
            "task-detail-card w-[1100px] max-w-[92vw] "
            "p-0 flex flex-col rounded-2xl overflow-hidden shadow-2xl"
        ).style("height: 80vh; max-height: 80vh;"):

            @ui.refreshable
            def dialog_body():
                t = selected.get("task")
                if not t:
                    ui.label("No task selected.").classes("p-8 text-slate-400 text-center")
                    return

                info_obj = load_task_info(t["dir"])
                status = t.get("status", "pending")
                icon_name = STATUS_ICONS.get(status, "help")

                # ── Dialog header (draggable) ──
                header = ui.row().classes(
                    "w-full bg-gradient-to-r from-slate-800 to-slate-900 "
                    "text-white px-6 py-3 items-center justify-between "
                    "cursor-move select-none flex-none"
                )
                header.on("mousedown", js_handler=_DRAG_JS)

                with header:
                    with ui.row().classes("items-center gap-3"):
                        ui.icon(icon_name, size="22px", color="white")
                        ui.label(t["name"]).classes("font-bold text-lg tracking-tight")
                        ui.icon("drag_indicator", size="16px").classes("text-white/30 ml-1")
                    with ui.row().classes("items-center gap-3"):
                        status_badge(status, size="md")
                        ui.button(icon="close", on_click=task_dialog.close).props(
                            "flat round dense color=white"
                        ).classes("opacity-70 hover:opacity-100")

                # ── Tabs ──
                tab_value = selected.get("tab", "task_info")
                with ui.tabs(value=tab_value).classes(
                    "w-full bg-slate-50 border-b border-slate-200 flex-none"
                ) as tabs:
                    ui.tab("task_info", label="Task Info", icon="info")
                    ui.tab("config", label="Config", icon="settings")
                    ui.tab("run.log", label="Run Log", icon="terminal")
                    ui.tab("notes", label="Notes", icon="edit_note")
                    ui.tab("env", label="Env Vars", icon="vpn_key")

                with ui.tab_panels(tabs, value=tab_value).classes(
                    "w-full flex-grow overflow-hidden"
                ).style("padding: 0; min-height: 0;"):

                    _build_tab_task_info(info_obj)
                    _build_tab_config(t)
                    _build_tab_run_log(t)
                    _build_tab_notes(t, info_obj)
                    _build_tab_env_vars(t, info_obj)

            dialog_body()

    def open_task_dialog(task, tab="task_info"):
        selected["task"] = task
        selected["tab"] = tab
        dialog_body.refresh()
        task_dialog.open()
        ui.run_javascript("""
            const card = document.querySelector('.task-detail-card');
            if (card) {
                card.style.transform = '';
                card.dataset.dragX = 0;
                card.dataset.dragY = 0;
            }
        """)

    selected["_dialog"] = task_dialog
    selected["_open_fn"] = open_task_dialog


# ═══════════════════════════════════════════════════════════════
#  Individual tab builders
# ═══════════════════════════════════════════════════════════════

def _build_tab_task_info(info_obj):
    """Task Info tab — readonly JSON viewer."""
    info_text = (
        json.dumps(info_obj, indent=2, ensure_ascii=False)
        if info_obj else "No task_info.json"
    )
    with ui.tab_panel("task_info"):
        readonly_code_viewer(info_text, mode="json")


def _build_tab_config(t):
    """Config tab — readonly YAML viewer."""
    import yaml as _yaml

    class _StrQuoteDumper(_yaml.SafeDumper):
        pass

    _StrQuoteDumper.add_representer(
        str,
        lambda dumper, data: dumper.represent_scalar(
            "tag:yaml.org,2002:str", data, style='"'
        ),
    )

    cfg_path = os.path.join(t["dir"], "config.yaml")
    try:
        cfg_data = load_yaml(cfg_path)
        cfg_text = _yaml.dump(
            cfg_data, Dumper=_StrQuoteDumper,
            default_flow_style=False,
            allow_unicode=True, sort_keys=False,
        ) if cfg_data else "# Empty config"
    except Exception:
        cfg_text = "# No config.yaml"

    with ui.tab_panel("config"):
        readonly_code_viewer(cfg_text, mode="yaml")


def _build_tab_run_log(t):
    """Run Log tab — log viewer with dropdown for run.log / rerunX.log."""
    with ui.tab_panel("run.log"):
        log_options = get_log_options(t["dir"])
        log_names = list(log_options.keys())

        if not log_names:
            with ui.column().classes("w-full h-full items-center justify-center"):
                ui.icon("article", size="48px").classes("text-slate-200")
                ui.label("No log files found.").classes("text-slate-400 mt-2")
        else:
            # Forward reference: dropdown callback needs to update CodeMirror
            log_cm_ref = [None]

            def _switch_log(e):
                cm = log_cm_ref[0]
                if not cm:
                    return
                path = log_options.get(e.value, "")
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as lf:
                        cm.value = lf.read()
                except Exception:
                    cm.value = f"Cannot read {e.value}"

            # Toolbar
            with ui.row().classes(
                "w-full items-center gap-3 px-4 py-1.5 flex-none "
                "bg-gradient-to-r from-slate-800 to-slate-900 "
                "border-b border-slate-700"
            ):
                ui.icon("terminal", size="16px").classes("text-emerald-400")
                ui.label("Log Viewer").classes(
                    "text-xs font-bold text-slate-300 tracking-wide"
                )
                ui.space()
                if len(log_names) > 1:
                    ui.select(
                        log_names, value=log_names[0],
                        on_change=_switch_log,
                    ).props("outlined dense dark rounded").classes("w-48")
                else:
                    ui.label(log_names[0]).classes("text-xs text-slate-400 font-mono")

            # Read initial content
            initial_path = log_options[log_names[0]]
            try:
                with open(initial_path, "r", encoding="utf-8", errors="replace") as lf:
                    initial_content = lf.read()
            except Exception:
                initial_content = f"Cannot read {log_names[0]}"

            # CodeMirror — flex fills remaining space via global CSS
            log_cm_ref[0] = ui.codemirror(
                value=initial_content, language=None, theme="vscodeDark",
                line_wrapping=True,
            ).classes("w-full readonly-cm")


def _build_tab_notes(t, info_obj):
    """Notes tab — editable textarea with save button."""
    with ui.tab_panel("notes"):
        notes_val = info_obj.get("notes", "") if isinstance(info_obj, dict) else ""
        notes_holder = {"text": notes_val}

        # Toolbar
        with ui.row().classes(
            "w-full items-center justify-between px-5 py-2 flex-none "
            "bg-gradient-to-r from-indigo-50 to-slate-50 "
            "border-b border-indigo-100"
        ):
            with ui.row().classes("items-center gap-2"):
                ui.icon("edit_note", size="18px").classes("text-indigo-500")
                ui.label("Task Notes & Description").classes(
                    "text-sm font-bold text-slate-700 tracking-wide"
                )

            def save_notes():
                info = load_task_info(t["dir"])
                info["notes"] = notes_holder["text"]
                save_task_info(t["dir"], info)
                ui.notify("Notes saved", type="positive", icon="check")

            ui.button("Save", icon="save", on_click=save_notes).props(
                "unelevated dense no-caps size=sm"
            ).classes("bg-indigo-600 text-white px-4 rounded-lg")

        # Textarea
        with ui.column().classes("w-full flex-grow overflow-auto px-4 py-3 bg-white"):
            notes_input = ui.textarea(
                value=notes_val,
                placeholder="Record experiment results, parameter notes, observations...",
            ).props("outlined autogrow").classes(
                "w-full font-mono text-sm"
            ).style("line-height: 1.8; min-height: 260px;")
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

