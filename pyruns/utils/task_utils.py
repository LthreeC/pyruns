"""
Validation utilities for Pyruns.
"""
import re
from typing import Optional

# Characters invalid in folder names (Windows + Unix)
_INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def validate_task_name(name: str) -> Optional[str]:
    """
    Validate whether a task name can be used as a folder name.
    Returns None if valid, or an error message string if invalid.
    """
    if not name or not name.strip():
        return "Task name cannot be empty"
    name = name.strip()
    if len(name) > 200:
        return "Task name is too long (max 200 characters)"
    bad = _INVALID_CHARS_RE.findall(name)
    if bad:
        return f"Task name contains invalid characters: {''.join(set(bad))}"
    if name in (".", ".."):
        return "Task name cannot be '.' or '..'"
    return None

