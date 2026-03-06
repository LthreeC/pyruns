"""
Centralized UI theme constants for Pyruns.
All style strings, color maps, and CSS classes live here.
"""

# ─── Layout Widths ──────────────────────────────────────────
SIDEBAR_WIDTH = "6%"            # Left navigation sidebar width
MONITOR_PANEL_WIDTH = "15%"      # Monitor page task list panel width

# ─── Spacing & Layout Presets ───────────────────────────────
# ═══════════════════════════════════════════════════════════════
#  UI Theme Constants
# ═══════════════════════════════════════════════════════════════
HEADER_GRADIENT = "bg-gradient-to-r from-[#0f172a] to-[#312e81]"

# ─── Header & Summary Bars ──────────────────────────────────
MANAGER_SUMMARY_BAR_CLASSES = (
    "w-full px-2 py-1 bg-white/95 backdrop-blur-sm border-b border-slate-200 mb-0 "
    "items-center justify-between shadow-md sticky top-0 z-[20]"
)
MANAGER_ACTION_ROW_CLASSES = (
    "w-full items-center justify-between gap-1 px-3 py-1.5 "
    "bg-white/95 backdrop-blur-sm border-b border-slate-200 shadow-sm z-[21]"
)
MANAGER_FILTER_ROW_CLASSES = (
    "w-full items-center gap-2 mb-0 px-3 py-1.5 bg-white/95 backdrop-blur-sm "
    "border-b border-slate-200 shadow-sm z-[22]"
)
MANAGER_GRID_CLASSES = "w-full gap-3 p-1"

# ─── Generator Page ─────────────────────────────────────────
GENERATOR_HEADER_CLASSES = (
    "w-full items-center gap-2 mb-2 px-3 py-1.5 bg-white "
    "border-b border-slate-200 sticky top-0 z-10 shadow-sm"
)
GENERATOR_WORKSPACE_CLASSES = "w-full gap-1 flex-nowrap items-start h-full"
GENERATOR_LEFT_COL_CLASSES = "flex-grow gap-2 min-w-0 h-full"
GENERATOR_RIGHT_COL_CLASSES = "w-96 flex-none sticky top-4"
GENERATOR_TOOLBAR_CLASSES = (
    "w-full items-center justify-between px-3 py-1 mb-1 "
    "bg-white border-b border-slate-200 shadow-sm"
)
GENERATOR_SETTINGS_CARD_CLASSES = "w-full p-3 shadow-sm bg-white border border-slate-100"
GENERATOR_TEMPLATE_SELECT_CLASSES = "w-64"
GENERATOR_VIEW_TOGGLE_WRAP_CLASSES = "items-center gap-1 bg-slate-100 p-1"
GENERATOR_FORM_SCROLL_CLASSES = "w-full overflow-y-auto flex-grow gap-0 generator-form-scroll"
GENERATOR_FORM_ROOT_CLASSES = "w-full gap-0 p-0 m-0"
GENERATOR_WARNING_ROW_CLASSES = "w-full items-start gap-2 px-3 py-2 bg-amber-50 border border-amber-200 mb-2"
GENERATOR_WARNING_ICON_CLASSES = "text-amber-600 mt-0.5"
GENERATOR_WARNING_TEXT_CLASSES = "text-xs text-amber-700 leading-5"
GENERATOR_YAML_EDITOR_CLASSES = "w-full overflow-hidden generator-yaml-editor"
GENERATOR_ARGS_CONTAINER_CLASSES = "w-full h-full p-4 bg-slate-50 border border-slate-200 gap-3"
GENERATOR_ARGS_EXPANSION_CLASSES = "w-full bg-white border border-slate-200"
GENERATOR_ARGS_RUNSCRIPT_INPUT_CLASSES = "w-full font-mono text-xs"
GENERATOR_ARGS_TEXTAREA_CLASSES = "w-full text-[12px] generator-args-textarea"
GENERATOR_GENERATE_BTN_CLASSES = "w-full text-sm font-bold py-2 tracking-wide rounded"
GENERATOR_ACTIVE_TOGGLE_BTN_CLASSES = "px-5 py-1.5 font-bold text-sm tracking-wide rounded"
GENERATOR_INACTIVE_TOGGLE_BTN_CLASSES = (
    "text-slate-500 font-medium px-5 py-1.5 text-sm hover:text-indigo-600 hover:bg-indigo-50/50"
)

PINNED_BLOCK_CLASSES = "w-full bg-indigo-50/30 border border-indigo-100 p-1 shadow-sm mb-0 gap-0"
PINNED_HEADER_CLASSES = "w-full items-center gap-1 px-1 pb-0 border-b border-indigo-100/50 mb-0.5"
PINNED_TITLE_CLASSES = "text-xs font-bold text-indigo-700 tracking-wide"
PINNED_EMPTY_CLASSES = "text-xs text-indigo-400 italic px-2 py-1"


def get_generator_batch_hint_html(separator: str, escape: str) -> str:
    # escape_html = escape.replace("\\", "\\\\")
    escape_html = escape
    return (
        '<span style="font-family:monospace;font-size:10px;color:#475569;">'
        '<b style="color:#6366f1;">Product</b>:&nbsp;'
        f'<code style="background:#e0e7ff;padding:0 3px;border-radius:2px;">lr: 0.001 {separator} 0.01 {separator} 0.1</code><br>'
        '<b style="color:#8b5cf6;">Zip</b>:&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;'
        f'<code style="background:#ede9fe;padding:0 3px;border-radius:2px;">seed: (1 {separator} 2 {separator} 3)</code><br>'
        '<b style="color:#ec4899;">Range</b>:&nbsp;&nbsp;&nbsp;'
        '<code style="background:#fce7f3;padding:0 3px;border-radius:2px;">epoch: 1:30:1</code><br>'
        '<b style="color:#14b8a6;">Escape</b>:&nbsp;&nbsp;'
        f'<span style="color:#64748b;">Use </span>'
        f'<code style="background:#ccfbf1;padding:0 3px;border-radius:2px;">{escape_html}</code>'
        f'<span style="color:#64748b;"> to escape {separator} in normal strings</span><br>'
        '<span style="color:#94a3b8;">Total = ∏(product) × zip_len</span>'
        '</span>'
    )

# ─── Placeholder Styles ─────────────────────────────────────
EMPTY_STATE_COL_CLASSES = "w-full items-center justify-center py-20"
EMPTY_STATE_ICON_SIZE = "48px"
EMPTY_STATE_ICON_CLASSES = "text-slate-200"
EMPTY_STATE_TEXT_CLASSES = "text-slate-400 italic mt-2"

MANAGER_EMPTY_ICON_SIZE = "72px"
MANAGER_EMPTY_ICON_CLASSES = "text-slate-200 mb-3"
MANAGER_EMPTY_TITLE_CLASSES = "text-xl font-bold text-slate-400"
MANAGER_EMPTY_SUB_CLASSES = "text-sm text-slate-400 mt-1"

MONITOR_EMPTY_COL_CLASSES = "w-full items-center py-8 gap-2"
MONITOR_EMPTY_ICON_SIZE = "32px"
MONITOR_EMPTY_ICON_CLASSES = "text-slate-200"
MONITOR_EMPTY_TEXT_CLASSES = "text-[10px] text-slate-400"

BATCH_HINT_BOX_CLASSES = "w-full gap-1 mb-3 px-2 py-1.5 bg-slate-50 border border-slate-100"
BATCH_HINT_TITLE_CLASSES = "text-[10px] font-bold text-slate-600"
BATCH_HINT_FOOTER_CLASSES = "text-[10px] text-slate-400 mt-3 text-center w-full"

# ─── Monitor Page ───────────────────────────────────────────
MONITOR_HEADER_HEIGHT_PX = 52
MONITOR_WORKSPACE_CLASSES = "w-full gap-0 flex-nowrap"
MONITOR_TERMINAL_COL_CLASSES = "flex-grow min-w-0 gap-0 overflow-hidden bg-[#1e1e1e] monitor-terminal-col"
MONITOR_WORKSPACE_STYLE = f"height: calc(100vh - {MONITOR_HEADER_HEIGHT_PX}px); overflow: hidden;"
MONITOR_SIDEBAR_STYLE = f"width: {MONITOR_PANEL_WIDTH}; height: 100%;"
MONITOR_HEADER_ROW_CLASSES = "w-full items-center gap-2 px-2 py-1.5 flex-none"
MONITOR_PINNED_CARD_CLASSES = "w-full gap-0 p-0 m-0 border-b-2 border-indigo-200 bg-indigo-50/50 flex-none"
MONITOR_TASK_LIST_SCROLL_CLASSES = "flex-grow w-full overflow-y-auto monitor-task-list-scroll"
MONITOR_EXPORT_BTN_CLASSES = "w-full text-sm font-bold tracking-wide py-1 rounded"
MONITOR_TASK_ITEM_BASE_CLASSES = (
    "flex-1 w-0 min-w-0 justify-center gap-0 cursor-pointer overflow-hidden "
    "py-1 pl-1.5 rounded monitor-task-item"
)

# ─── Input / Form ───────────────────────────────────────────
INPUT_PROPS = "outlined dense bg-white text-slate-800"
INPUT_BORDERLESS_PROPS = "dense borderless bg-color=white"
INPUT_OUTLINED_CLASSES = "flex-grow ml-2 px-2 rounded-md shadow-sm border border-slate-200"
ICON_BTN_MUTED_CLASSES = "text-slate-400 hover:text-indigo-500"
TEXT_MONO_XS = "text-xs font-mono"

# ─── Typography & Shared Element Styles ──────────────────────
LABEL_BOLD_TRACKING = "text-sm font-bold text-slate-700 tracking-wide"
TAB_PANEL_FULL = "p-0 gap-0 flex flex-col h-full w-full"
TEXTAREA_FULL_BORDERLESS = "w-full h-full font-mono text-sm px-5 py-4 outline-none focus:outline-none"
EMPTY_LABEL_CLASSES = "p-8 text-slate-400 text-center"
ICON_BTN_HOVER_OPACITY = "opacity-70 hover:opacity-100"

# ─── Tabs ──────────────────────────────────────────────────
TABS_PROPS = "dense align=center no-caps active-color=indigo active-bg-color=indigo-50 indicator-color=indigo"
TABS_CLASSES = "w-full bg-slate-100 text-slate-500 border-b border-slate-200 flex-none"
TAB_PANELS_CLASSES = "w-full flex-grow overflow-hidden relative"
TAB_CONTENT_CLASSES = "w-full flex-grow p-0 m-0 bg-white overflow-hidden"

# ─── Button Styles ──────────────────────────────────────────
BTN_CLASS = "transition-all duration-200"
BTN_PRIMARY = f"{BTN_CLASS} bg-indigo-600 text-white hover:bg-indigo-700 shadow-sm hover:shadow"
BTN_SUCCESS = f"{BTN_CLASS} bg-emerald-600 text-white hover:bg-emerald-700 shadow-sm hover:shadow"
BTN_DANGER = f"{BTN_CLASS} bg-rose-600 text-white hover:bg-rose-700 shadow-sm hover:shadow"
BTN_RUN_SELECTED_CLASSES = f"{BTN_SUCCESS} font-bold px-6 py-2.5 text-sm"
BTN_DELETE_SELECTED_CLASSES = BTN_DANGER

# ─── Shared Panel Styles ──────────────────────────────────
PANEL_HEADER_INDIGO = (
    "bg-gradient-to-r from-indigo-600 to-indigo-700"
)
PANEL_HEADER_DARK = (
    "bg-gradient-to-r from-slate-800 to-slate-900 border-b border-slate-700"
)

# ─── Dialogs & Modals ──────────────────────────────────────
DIALOG_BACKDROP = "w-full max-w-[70vw] p-0 flex flex-col overflow-hidden shadow-2xl"
DIALOG_WIDTH_LARGE = "w-[1100px]"
DIALOG_HEADER_DARK = "w-full bg-gradient-to-r from-slate-800 to-slate-900 text-white px-6 py-3 items-center justify-between flex-none"
DIALOG_HEADER_PRIMARY = "w-full bg-gradient-to-r from-indigo-600 to-indigo-700 text-white px-6 py-4 items-center justify-between flex-none"
DIALOG_TITLE_CLASSES = "font-bold text-lg tracking-tight truncate"

# ─── Loading States ────────────────────────────────────────
LOADING_OVERLAY_CLASSES = "absolute inset-0 items-center justify-center bg-white/70 z-10 gap-3"
LOADING_TEXT_CLASSES = "text-slate-500 font-medium"

# ─── Toolbars & Sub-headers ────────────────────────────────
TOOLBAR_LIGHT = "w-full items-center justify-between px-5 py-2 flex-none bg-gradient-to-r from-indigo-50 to-slate-50 border-b border-indigo-100"
TOOLBAR_DARK = "w-full items-center gap-3 px-4 py-1.5 flex-none bg-gradient-to-r from-slate-800 to-slate-900 border-b border-slate-700"

EXPORT_PRE_STYLE = "color:#94a3b8;font-size:11px;padding:8px 12px;background:#1e293b;border-radius:8px;margin:4px 0;"

# ─── Task Status Colors ────────────────────────────────────
STATUS_CARD_STYLES = {
    "pending":     "border-slate-200 bg-white",
    "queued":      "border-blue-200 bg-blue-50/30",
    "running":     "border-amber-300 bg-amber-50/40 shadow-md",
    "completed":   "border-emerald-200 bg-emerald-50/20",
    "failed":      "border-rose-200 bg-rose-50/20",
}

STATUS_BADGE_STYLES = {
    "pending":     "bg-slate-100 text-slate-500",
    "queued":      "bg-blue-100 text-blue-700",
    "running":     "bg-amber-100 text-amber-700 animate-pulse",
    "completed":   "bg-emerald-100 text-emerald-700",
    "failed":      "bg-rose-100 text-rose-700",
}

STATUS_ICONS = {
    "pending":     "schedule",
    "queued":      "hourglass_top",
    "running":     "play_circle",
    "completed":   "check_circle",
    "failed":      "error",
}

STATUS_ICON_COLORS = {
    "pending":     "text-slate-400",
    "queued":      "text-blue-500",
    "running":     "text-amber-500",
    "completed":   "text-emerald-500",
    "failed":      "text-rose-500",
}

# ─── Task Card ──────────────────────────────────────────────
CARD_BASE_CLASSES = "w-full border shadow-sm hover:shadow-lg transition-all duration-200 cursor-pointer group p-0 overflow-hidden"
CARD_HEADER_CLASSES = "w-full items-start px-3 pt-3 pb-0 gap-2 flex-nowrap"
CARD_TITLE_COL_CLASSES = "flex-grow gap-0.5 min-w-0"
CARD_TITLE_CLASSES = "font-bold text-[13px] text-slate-800 group-hover:text-indigo-700 transition-colors truncate leading-snug"
CARD_TIME_CLASSES = "text-[10px] text-slate-400 font-mono leading-tight"

PIN_ACTIVE_CLASSES = "text-amber-500 opacity-100"
PIN_INACTIVE_CLASSES = "text-slate-300 opacity-0 group-hover:opacity-60"

CARD_BADGE_ROW_CLASSES = "w-full px-3 py-0.5"
CARD_BODY_CLASSES = "w-full px-3 py-1.5 gap-0.5 flex-grow"
CARD_CONFIG_LINE_CLASSES = "text-[11px] text-slate-500 truncate w-full font-mono leading-relaxed"

CARD_FOOTER_CLASSES = "w-full items-center justify-between px-3 py-2 mt-auto border-t border-slate-100 bg-slate-50/60"

CHECKBOX_PROPS = "dense color=indigo size=xs"
ACTION_BTN_PROPS = "flat round dense size=sm"
ACTION_BTN_CLASSES = "text-slate-400 hover:text-indigo-600 hover:bg-indigo-50 transition-colors"

# =============================================================================
# Common Reusable Aliases (high-frequency classes/props)
# =============================================================================
ROW_CENTER_GAP_1 = "items-center gap-1"
ROW_CENTER_GAP_2 = "items-center gap-2"
ROW_CENTER_GAP_3 = "items-center gap-3"
ROW_CENTER_GAP_4 = "items-center gap-4"
COL_FULL = "w-full"
TEXT_HEADING_SM = "text-sm font-bold text-slate-700 tracking-wide"
TEXT_MUTED_XS = "text-xs text-slate-500"

FILTER_SELECT_CLASSES = "w-48"
SEARCH_TEXTAREA_CLASSES = "w-56 font-mono search-textarea"
WORKERS_INPUT_CLASSES = "w-20"
MODE_SELECT_CLASSES = "w-28"
COMPACT_SELECT_CLASSES = "w-24"
REFRESH_ICON_BTN_PROPS = "flat round dense color=slate"
DIR_PICKER_APPEND_BTN_PROPS = "flat round dense"
DIR_PICKER_APPEND_BTN_CLASSES = "text-slate-400 hover:text-indigo-600"

MANAGER_LOADING_COL_CLASSES = "w-full items-center justify-center py-20 gap-4 mt-10"
MANAGER_LOADING_TEXT_CLASSES = "text-slate-500 font-bold tracking-wider animate-pulse"
MANAGER_BODY_CLASSES = "w-full flex-grow px-1 pb-1 gap-1 overflow-y-auto"

MONITOR_TERMINAL_INNER_CLASSES = "w-full flex-grow overflow-hidden monitor-terminal-inner"
MONITOR_TERMINAL_CLASSES = "w-full h-full pl-2 pt-1 monitor-terminal"
MONITOR_HEADER_ICON_CLASSES = "text-slate-500"
MONITOR_HEADER_LABEL_CLASSES = "text-xs font-bold text-white truncate"
MONITOR_LOG_SELECT_PROPS = "outlined dense dark options-dense"
MONITOR_LOG_SELECT_CLASSES = "w-36"
MONITOR_SIDEBAR_CLASSES = "flex-none border-r border-slate-200 bg-white gap-0 overflow-hidden"
MONITOR_SIDEBAR_HEADER_CLASSES = "w-full items-center gap-1 px-1 py-2 flex-none"
MONITOR_SEARCH_ROW_CLASSES = (
    "w-full px-0 py-1 flex-none border-b border-slate-100 items-center gap-1 flex-nowrap overflow-hidden"
)
MONITOR_LIST_COL_CLASSES = "w-full gap-0 p-0 m-0 overflow-hidden shrink-0"
MONITOR_EXPORT_ROW_CLASSES = "w-full p-2 flex-none mt-auto"
MONITOR_TASK_ROW_CLASSES = (
    "w-full max-w-full items-center gap-0.5 flex-nowrap min-w-0 overflow-hidden border-b border-slate-50 pr-2"
)

SIDEBAR_COL_CLASSES = (
    "flex-none bg-white border-r border-slate-100 gap-0 "
    "shadow-[4px_0_24px_rgba(0,0,0,0.02)] print:hidden"
)
SIDEBAR_MENU_TITLE_CLASSES = "text-[9px] font-bold text-slate-400 px-2 mt-5 mb-2 tracking-wider"
SIDEBAR_LIST_CLASSES = "w-full gap-0 px-1.5"
SIDEBAR_BTN_PROPS = "flat no-caps"
SIDEBAR_ICON_ACTIVE = "text-indigo-600 text-sm"
SIDEBAR_ICON_INACTIVE = "text-slate-400 text-sm"
SIDEBAR_LABEL_CLASSES = "truncate"

TASK_CARD_ERROR_CLASSES = "text-red-500 font-bold p-4"
TASK_CARD_CHECKBOX_CLASSES = "mt-0.5 task-checkbox"
TASK_CARD_META_ROW_CLASSES = "items-center gap-1 mt-0.5"
TASK_CARD_META_ICON_CLASSES = "text-indigo-400"
TASK_CARD_META_TEXT_CLASSES = "text-[10px] font-mono text-indigo-400"
TASK_CARD_ACTIONS_LEFT_CLASSES = "items-center gap-1"
TASK_CARD_RUNNING_ACTIONS_CLASSES = "items-center gap-1.5"

# =============================================================================
# Env Editor
# =============================================================================
ENV_EDITOR_ROOT_CLASSES = "w-full h-full gap-0"
ENV_EDITOR_TOOLBAR_TITLE_CLASSES = "text-sm font-bold text-slate-700 tracking-wide"
ENV_EDITOR_TOOLBAR_ACTIONS_CLASSES = "items-center gap-1.5"
ENV_EDITOR_ADD_BTN_CLASSES = "text-indigo-600 hover:bg-indigo-100 px-2"
ENV_EDITOR_SAVE_BTN_CLASSES = "px-4"
ENV_EDITOR_TABLE_HEAD_CLASSES = (
    "w-full items-center bg-slate-100 gap-0 flex-none border-b border-slate-200"
)
ENV_EDITOR_TABLE_BODY_CLASSES = "w-full gap-0 flex-grow overflow-auto bg-white"
ENV_EDITOR_EMPTY_COL_CLASSES = "w-full items-center justify-center py-20 gap-4"
ENV_EDITOR_EMPTY_ICON_CLASSES = "text-indigo-200"
ENV_EDITOR_EMPTY_TITLE_CLASSES = "text-slate-400 text-base"
ENV_EDITOR_EMPTY_SUB_CLASSES = "text-slate-300 text-xs"
ENV_EDITOR_EMPTY_ADD_CLASSES = "text-indigo-500 border-indigo-300 mt-2 px-4"
ENV_EDITOR_KEY_INPUT_CLASSES = "w-[35%] font-mono text-[13px] text-slate-800"
ENV_EDITOR_VAL_INPUT_CLASSES = "flex-grow font-mono text-[13px] text-slate-700"
ENV_EDITOR_DEL_BTN_CLASSES = "w-8 text-slate-300 hover:text-red-500 transition-all"

ADD_CLASSES = "text-indigo-500 border-indigo-300 mt-2 px-4"
ENV_EDITOR_KEY_INPUT_CLASSES = "w-[35%] font-mono text-[13px] text-slate-800"
ENV_EDITOR_VAL_INPUT_CLASSES = "flex-grow font-mono text-[13px] text-slate-700"
ENV_EDITOR_DEL_BTN_CLASSES = "w-8 text-slate-300 hover:text-red-500 transition-all"

ADD_CLASSES = "text-indigo-500 border-indigo-300 mt-2 px-4"
ENV_EDITOR_KEY_INPUT_CLASSES = "w-[35%] font-mono text-[13px] text-slate-800"
ENV_EDITOR_VAL_INPUT_CLASSES = "flex-grow font-mono text-[13px] text-slate-700"
ENV_EDITOR_DEL_BTN_CLASSES = "w-8 text-slate-300 hover:text-red-500 transition-all"

ADD_CLASSES = "text-indigo-500 border-indigo-300 mt-2 px-4"
ENV_EDITOR_KEY_INPUT_CLASSES = "w-[35%] font-mono text-[13px] text-slate-800"
ENV_EDITOR_VAL_INPUT_CLASSES = "flex-grow font-mono text-[13px] text-slate-700"
ENV_EDITOR_DEL_BTN_CLASSES = "w-8 text-slate-300 hover:text-red-500 transition-all"

