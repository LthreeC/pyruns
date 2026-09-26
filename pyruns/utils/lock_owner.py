"""JSON file-lock owners and completion markers for delayed deletion."""
import json
import os
import socket
from typing import Any


RELEASED_LOCK_SUFFIX = b" released"
_LOCK_OWNER_HOST = socket.gethostname().lower()


def parse_json_lock_owner(content: bytes) -> dict[str, Any] | None:
    try:
        owner = json.loads(content.removesuffix(RELEASED_LOCK_SUFFIX).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(owner, dict):
        return None
    pid, host, token = owner.get("pid"), owner.get("host"), owner.get("token")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if not isinstance(host, str) or not host or not isinstance(token, str) or not token:
        return None
    return owner


def mark_json_lock_released(fd: int, expected_owner: bytes | None = None) -> bytes:
    """Mark only the original open owner; return bytes for exact cleanup checks."""
    fallback = expected_owner if expected_owner is not None else b""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        content = os.read(fd, 4097)
        if len(content) > 4096 or (expected_owner is not None and content != expected_owner):
            return fallback
        owner = parse_json_lock_owner(content)
        if owner is None or owner["pid"] != os.getpid() or owner["host"].lower() != _LOCK_OWNER_HOST:
            return fallback
        if content.endswith(RELEASED_LOCK_SUFFIX):
            return content
        os.lseek(fd, 0, os.SEEK_END)
        written = os.write(fd, RELEASED_LOCK_SUFFIX)
        return content + RELEASED_LOCK_SUFFIX[:written]
    except OSError:
        return fallback
