"""Low-level log file I/O helpers used by the Monitor and log endpoints."""

from __future__ import annotations

import locale
import os
from typing import Tuple


def _log_decode_candidates() -> list[str]:
    """Return preferred encodings for persisted log files."""

    candidates = ["utf-8-sig", "utf-8"]
    preferred = str(locale.getpreferredencoding(False) or "").strip()
    if preferred and preferred.lower() not in {item.lower() for item in candidates}:
        candidates.append(preferred)

    if os.name == "nt":
        for encoding in ("gbk", "cp936", "cp65001"):
            if encoding.lower() not in {item.lower() for item in candidates}:
                candidates.append(encoding)

    return candidates


def _decode_with_encoding(data: bytes, encoding: str, *, errors: str = "strict") -> str | None:
    """Decode ``data`` with one encoding, returning ``None`` when it fails."""

    try:
        return data.decode(encoding, errors=errors)
    except (LookupError, UnicodeDecodeError):
        return None


def decode_log_bytes(data: bytes) -> str:
    """Decode raw log bytes with UTF-8 first and sensible Windows fallbacks."""

    if not data:
        return ""

    for encoding in _log_decode_candidates():
        text = _decode_with_encoding(data, encoding)
        if text is not None:
            return text

    utf8_replace = _decode_with_encoding(data, "utf-8", errors="replace") or ""
    # Tail reads can start mid-codepoint; prefer UTF-8 when only a few bytes break.
    if utf8_replace.count("\ufffd") <= max(2, len(data) // 2048):
        return utf8_replace

    fallback_candidates: list[tuple[int, int, str]] = []
    for rank, encoding in enumerate(_log_decode_candidates()):
        text = _decode_with_encoding(data, encoding, errors="replace")
        if text is not None:
            fallback_candidates.append((text.count("\ufffd"), rank, text))

    if fallback_candidates:
        fallback_candidates.sort(key=lambda item: (item[0], item[1]))
        return fallback_candidates[0][2]

    return data.decode("utf-8", errors="replace")


def normalize_log_newlines(text: str) -> str:
    """Leave terminal/log text unchanged; xterm handles end-of-line rendering."""

    return text or ""


def append_log(log_path: str, message: str) -> None:
    """Append text to a log file safely."""

    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(message)
    except Exception:
        pass


def read_log(log_path: str) -> str:
    """Read the full decoded log content from disk."""

    if not os.path.exists(log_path):
        return ""

    try:
        with open(log_path, "rb") as handle:
            return normalize_log_newlines(decode_log_bytes(handle.read()))
    except Exception:
        return ""


def read_log_chunk(log_path: str, offset: int) -> Tuple[str, int]:
    """Read new content from log file starting at ``offset``."""

    if not os.path.exists(log_path):
        return "", 0

    try:
        size = os.path.getsize(log_path)
        if size < offset:
            offset = 0

        with open(log_path, "rb") as handle:
            handle.seek(offset)
            new_bytes = handle.read()
            new_offset = handle.tell()

        if not new_bytes:
            return "", new_offset

        return normalize_log_newlines(decode_log_bytes(new_bytes)), new_offset
    except Exception:
        return "", offset


def read_last_bytes(log_path: str, n_bytes: int = 10000) -> Tuple[str, int]:
    """Read the last ``n_bytes`` from a log file."""

    if not os.path.exists(log_path):
        return "", 0

    try:
        size = os.path.getsize(log_path)
        start = max(0, size - n_bytes)

        with open(log_path, "rb") as handle:
            handle.seek(start)
            content = normalize_log_newlines(decode_log_bytes(handle.read()))
            return content, size
    except Exception:
        return "", 0


def _split_lf_lines_keepends(data: bytes) -> list[bytes]:
    """Split bytes into LF-delimited records without treating CR as a line break."""

    if not data:
        return []

    lines: list[bytes] = []
    start = 0
    while True:
        index = data.find(b"\n", start)
        if index < 0:
            if start < len(data):
                lines.append(data[start:])
            break
        lines.append(data[start:index + 1])
        start = index + 1
    return lines


def read_last_lines(log_path: str, max_lines: int = 10000, max_bytes: int | None = None) -> Tuple[str, int]:
    """Read up to the last ``max_lines`` LF-delimited log lines."""

    if not os.path.exists(log_path):
        return "", 0

    try:
        size = os.path.getsize(log_path)
        if size <= 0:
            return "", 0

        max_lines = max(0, int(max_lines))
        if max_lines == 0:
            return "", size
        byte_limit = None if max_bytes is None else max(1, int(max_bytes))

        block_size = 64 * 1024
        position = size
        chunks: list[bytes] = []
        line_break_count = 0
        bytes_read = 0

        with open(log_path, "rb") as handle:
            while position > 0 and line_break_count <= max_lines and (
                byte_limit is None or bytes_read < byte_limit
            ):
                read_size = min(block_size, position)
                if byte_limit is not None:
                    read_size = min(read_size, byte_limit - bytes_read)
                position -= read_size
                handle.seek(position)
                chunk = handle.read(read_size)
                chunks.append(chunk)
                bytes_read += len(chunk)
                line_break_count += chunk.count(b"\n")

        data = b"".join(reversed(chunks))
        lines = _split_lf_lines_keepends(data)
        if len(lines) > max_lines:
            data = b"".join(lines[-max_lines:])

        return normalize_log_newlines(decode_log_bytes(data)), size
    except Exception:
        return "", 0


def safe_read_log(filepath: str, offset: int, max_bytes: int = 50000) -> Tuple[str, int]:
    """Read up to ``max_bytes`` from ``filepath`` at ``offset`` safely."""

    try:
        if not os.path.exists(filepath):
            return "", offset

        file_size = os.path.getsize(filepath)
        if offset >= file_size:
            return "", file_size

        with open(filepath, "rb") as handle:
            handle.seek(offset)
            chunk = handle.read(max_bytes)

            if not chunk:
                return "", offset

            if offset + len(chunk) < file_size:
                last_newline = chunk.rfind(b"\n")
                if last_newline != -1:
                    chunk = chunk[: last_newline + 1]

            return normalize_log_newlines(decode_log_bytes(chunk)), offset + len(chunk)
    except OSError:
        return "", offset
