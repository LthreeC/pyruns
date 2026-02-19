"""
Low-level log file I/O helpers (append, read).

Moved from core/ to utils/ — these are pure I/O utilities, not business logic.
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
    """Read log content safely (legacy full read)."""
    if not os.path.exists(log_path):
        return ""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def read_log_chunk(log_path: str, offset: int) -> tuple[str, int]:
    """
    Read new content from log file starting at `offset`.
    
    Returns:
        (content, new_offset)
    
    If file was truncated (size < offset), reads from beginning.
    """
    if not os.path.exists(log_path):
        return "", 0
        
    try:
        # Check size first to detect truncation
        size = os.path.getsize(log_path)
        if size < offset:
            offset = 0  # file truncated/rotated -> reset
            
        with open(log_path, "rb") as f:
            f.seek(offset)
            new_bytes = f.read()
            new_offset = f.tell()
            
        if not new_bytes:
            return "", new_offset
            
        return new_bytes.decode("utf-8", errors="replace"), new_offset
    except Exception:
        return "", offset


def read_last_bytes(log_path: str, n_bytes: int = 10000) -> tuple[str, int]:
    """
    Read the last `n_bytes` of a log file.
    
    Returns:
        (content, new_offset) where new_offset is at the END of the file.
    """
    if not os.path.exists(log_path):
        return "", 0
        
    try:
        size = os.path.getsize(log_path)
        start = max(0, size - n_bytes)
        
        with open(log_path, "rb") as f:
            f.seek(start)
            content = f.read().decode("utf-8", errors="replace")
            # If we started in middle, we might have partial line/char garbage at start?
            # Ideally we skip to first newline, but for now simple tail is fine.
            return content, size
    except Exception:
        return "", 0


def safe_read_log(filepath: str, offset: int, max_bytes: int = 50000) -> tuple[str, int]:
    """
    Read up to `max_bytes` from file at `offset`.
    
    Ensures safe UTF-8 decoding by back-tracking to the last newline if a chunk
    boundary splits a multi-byte character or ANSI sequence.
    
    Returns:
        (text, new_offset)
    """
    if not os.path.exists(filepath):
        return "", offset
        
    file_size = os.path.getsize(filepath)
    if offset >= file_size:
        return "", file_size
        
    with open(filepath, "rb") as f:
        f.seek(offset)
        chunk = f.read(max_bytes)
        
        if not chunk:
            return "", offset
            
        # If we didn't read until the end, backtrack to last newline
        # to ensure we don't present partial lines/colors
        if offset + len(chunk) < file_size:
            last_newline = chunk.rfind(b'\n')
            if last_newline != -1:
                chunk = chunk[:last_newline + 1]
                
        # Decode and normalize newlines for xterm.js
        text = chunk.decode("utf-8", errors="replace").replace('\n', '\r\n')
        return text, offset + len(chunk)
