"""
Centralized UI theme constants for Pyruns.
All style strings, color maps, and CSS classes live here.
"""

# ─── Layout Widths ──────────────────────────────────────────
SIDEBAR_WIDTH = "10%"            # Left navigation sidebar width
MONITOR_PANEL_WIDTH = "16%"      # Monitor page task list panel width

# ─── Spacing & Layout Presets ───────────────────────────────
LAYOUT_ROW_GAP = "gap-2"
LAYOUT_COL_GAP = "gap-2"
CARD_MIN_HEIGHT = "148px"

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
MONITOR_TERMINAL_COL_CLASSES = "flex-grow min-w-0 gap-0 overflow-hidden bg-[#1e1e1e]"

# ─── Input / Form ───────────────────────────────────────────
INPUT_PROPS = "outlined dense bg-white text-slate-800"

# ─── Button Styles ──────────────────────────────────────────
BTN_CLASS = "transition-all duration-200"
BTN_PRIMARY = f"{BTN_CLASS} bg-indigo-600 text-white hover:bg-indigo-700 shadow-sm hover:shadow"
BTN_SUCCESS = f"{BTN_CLASS} bg-emerald-600 text-white hover:bg-emerald-700 shadow-sm hover:shadow"
BTN_DANGER = f"{BTN_CLASS} bg-rose-600 text-white hover:bg-rose-700 shadow-sm hover:shadow"
BTN_OUTLINE = f"{BTN_CLASS} border border-slate-300 text-slate-700 hover:bg-slate-50 font-medium"

# ─── Shared Panel Styles ──────────────────────────────────
PANEL_CARD = "bg-white shadow-sm border border-slate-100"
PANEL_HEADER_INDIGO = (
    "bg-gradient-to-r from-indigo-600 to-indigo-700"
)
PANEL_HEADER_DARK = (
    "bg-gradient-to-r from-slate-800 to-slate-900 border-b border-slate-700"
)
DARK_BG = "#0d1117"

EXPORT_PRE_STYLE = "color:#94a3b8;font-size:11px;padding:8px 12px;background:#1e293b;border-radius:8px;margin:4px 0;"
PARAM_CARD_ALT_BG = "background:#fffbeb"
PARAM_CARD_BG = "background:#fafbfc"

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

# ─── Status sort order (shared by manager + monitor) ──────
STATUS_ORDER = {
    "running": 0, "queued": 1, "pending": 2,
    "failed": 3, "completed": 4,
}

