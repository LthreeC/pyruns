"""Console output helpers for CLI commands."""

from __future__ import annotations

import sys
from typing import TextIO


def write_console_text(text: str, stream: TextIO | None = None) -> None:
    """Write text to a console stream, replacing unencodable characters."""

    target = stream or sys.stdout
    try:
        target.write(text)
    except UnicodeEncodeError:
        encoding = getattr(target, "encoding", None) or sys.getdefaultencoding() or "utf-8"
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        target.write(safe_text)
