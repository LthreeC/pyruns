"""
Reusable UI helper functions (involve UI but are shared across pages).
"""
from nicegui import ui


def choose_directory(initial_dir: str = "") -> str | None:
    """
    Open a native folder picker dialog.
    Returns the selected path, or None if cancelled / unavailable.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(initialdir=initial_dir)
        root.destroy()
        return path if path else None
    except Exception:
        ui.notify("Folder picker unavailable in this environment.", type="warning")
        return None

