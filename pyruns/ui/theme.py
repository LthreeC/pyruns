"""
Centralized UI theme constants for Pyruns.
All style strings, color maps, and CSS classes live here.
"""

# ─── Layout ─────────────────────────────────────────────────
SIDEBAR_WIDTH = "15%"

# ─── Input / Form ───────────────────────────────────────────
INPUT_PROPS = "outlined dense bg-white text-slate-800"
BTN_CLASS = "transition-all duration-200"

# ─── Shared Panel Styles ──────────────────────────────────
PANEL_CARD = "bg-white shadow-sm border border-slate-100"
PANEL_HEADER_INDIGO = (
    "bg-gradient-to-r from-indigo-600 to-indigo-700"
)
PANEL_HEADER_DARK = (
    "bg-gradient-to-r from-slate-800 to-slate-900 border-b border-slate-700"
)
DARK_BG = "#0d1117"

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

