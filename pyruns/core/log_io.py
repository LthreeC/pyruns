"""
Low-level log file I/O helpers (append, read).
"""
import os


def append_log(log_path: str, message: str) -> None:
    """Append text to a log file safely."""
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(message)
    except Exception:
        pass


def read_log(log_path: str) -> str:
    """Read log content safely (handles mixed encodings gracefully)."""
    if not os.path.exists(log_path):
        return ""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""

