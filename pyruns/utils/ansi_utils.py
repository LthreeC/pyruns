"""
ANSI escape code → HTML converter.
Supports standard 8/16 color codes for terminal-like rendering in the browser.
"""
import re
from typing import List

# ── Standard ANSI foreground colors ──────────────────────────
_FG_COLORS = {
    "30": "#6b7280",  # black → gray (visible on dark bg)
    "31": "#ef4444",  # red
    "32": "#22c55e",  # green
    "33": "#eab308",  # yellow
    "34": "#60a5fa",  # blue
    "35": "#c084fc",  # magenta
    "36": "#22d3ee",  # cyan
    "37": "#e2e8f0",  # white
    # Bright variants
    "90": "#9ca3af",  # bright black (gray)
    "91": "#f87171",  # bright red
    "92": "#4ade80",  # bright green
    "93": "#facc15",  # bright yellow
    "94": "#93c5fd",  # bright blue
    "95": "#d8b4fe",  # bright magenta
    "96": "#67e8f9",  # bright cyan
    "97": "#f8fafc",  # bright white
}

_BG_COLORS = {
    "40": "#1f2937", "41": "#7f1d1d", "42": "#14532d", "43": "#713f12",
    "44": "#1e3a5f", "45": "#581c87", "46": "#164e63", "47": "#374151",
    "100": "#374151", "101": "#991b1b", "102": "#166534", "103": "#854d0e",
    "104": "#1e40af", "105": "#6b21a8", "106": "#155e75", "107": "#4b5563",
}

# Regex: matches ESC[ ... m  (SGR sequences)
_ANSI_RE = re.compile(r"\033\[([0-9;]*)m")


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def ansi_to_html(text: str) -> str:
    """Convert ANSI escape codes in *text* to HTML ``<span>`` elements.

    Returns HTML safe for embedding inside a ``<pre>`` block with a dark
    background.  Unrecognised sequences are silently stripped.
    """
    parts: List[str] = []
    open_spans = 0
    last = 0

    for m in _ANSI_RE.finditer(text):
        # Append plain text before this escape
        parts.append(_escape_html(text[last : m.start()]))
        last = m.end()

        codes = m.group(1).split(";") if m.group(1) else ["0"]

        for code in codes:
            if code in ("0", ""):
                # Reset – close all open spans
                parts.append("</span>" * open_spans)
                open_spans = 0
            elif code == "1":
                parts.append('<span style="font-weight:bold;">')
                open_spans += 1
            elif code == "2":
                parts.append('<span style="opacity:0.7;">')
                open_spans += 1
            elif code == "3":
                parts.append('<span style="font-style:italic;">')
                open_spans += 1
            elif code == "4":
                parts.append('<span style="text-decoration:underline;">')
                open_spans += 1
            elif code in _FG_COLORS:
                parts.append(f'<span style="color:{_FG_COLORS[code]};">')
                open_spans += 1
            elif code in _BG_COLORS:
                parts.append(
                    f'<span style="background:{_BG_COLORS[code]};'
                    f'border-radius:2px;padding:0 2px;">'
                )
                open_spans += 1
            # else: unknown code → ignore

    # Remaining text
    parts.append(_escape_html(text[last:]))
    # Close any leftover spans
    parts.append("</span>" * open_spans)

    return "".join(parts)


def tail_lines(text: str, n: int = 1000) -> str:
    """Return the last *n* lines of *text*."""
    lines = text.split("\n")
    if len(lines) <= n:
        return text
    return "\n".join(lines[-n:])

