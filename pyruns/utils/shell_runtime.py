"""Shell runtime detection and settings resolution for shell tasks."""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from typing import Any, Dict

from pyruns._config import ENV_KEY_SHELL
from pyruns.utils.settings import load_settings

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is available in normal installs
    psutil = None  # type: ignore[assignment]


SHELL_MODE_FOLLOW = "follow"
SHELL_MODE_CUSTOM = "custom"

_SHELL_DISPLAY_NAMES = {
    "powershell": "PowerShell",
    "cmd": "Command Prompt",
    "bash": "Bash",
    "sh": "Shell",
    "zsh": "Zsh",
    "fish": "Fish",
}

_SHELL_KIND_ALIASES = {
    "powershell": "powershell",
    "powershell.exe": "powershell",
    "pwsh": "powershell",
    "pwsh.exe": "powershell",
    "cmd": "cmd",
    "cmd.exe": "cmd",
    "bash": "bash",
    "bash.exe": "bash",
    "sh": "sh",
    "sh.exe": "sh",
    "dash": "sh",
    "zsh": "zsh",
    "zsh.exe": "zsh",
    "fish": "fish",
    "fish.exe": "fish",
}


def normalize_shell_mode(value: Any) -> str:
    """Normalize shell mode to one of the supported values."""

    return SHELL_MODE_CUSTOM if str(value or "").strip().lower() == SHELL_MODE_CUSTOM else SHELL_MODE_FOLLOW


def classify_shell_executable(candidate: str) -> tuple[str, str]:
    """Return ``(kind, display_name)`` for a shell executable path or name."""

    name = os.path.basename(str(candidate or "")).strip().lower()
    kind = _SHELL_KIND_ALIASES.get(name, "unknown")
    display = _SHELL_DISPLAY_NAMES.get(kind, name or "Unknown shell")
    return kind, display


def _resolve_candidate_path(candidate: str) -> str:
    """Resolve a shell executable candidate to a concrete path when possible."""

    normalized = str(candidate or "").strip()
    if not normalized:
        return ""
    if os.path.isabs(normalized):
        return normalized if os.path.exists(normalized) else ""
    resolved = shutil.which(normalized)
    if resolved and os.path.exists(resolved):
        return resolved
    return ""


def _shell_settings_root_for_task(task_dir: str | None = None) -> str | None:
    """Return the settings root that applies to a task directory."""

    if not task_dir:
        return None
    return os.path.dirname(os.path.dirname(os.path.abspath(task_dir)))


def _load_shell_preferences(settings_root: str | None = None) -> tuple[str, str]:
    """Load shell settings from the workspace root."""

    settings = load_settings(settings_root) if settings_root else load_settings()
    mode = normalize_shell_mode(settings.get("shell_mode"))
    shell_executable = str(settings.get("shell_executable", "") or "").strip()
    return mode, shell_executable


def _find_shell_in_process_tree() -> Dict[str, str] | None:
    """Inspect the current process ancestry to find the launching shell."""

    if psutil is None:
        return None

    try:
        lineage = [psutil.Process(os.getpid()), *psutil.Process(os.getpid()).parents()]
    except Exception:
        return None

    for proc in lineage:
        try:
            name = proc.name().lower()
        except Exception:
            continue
        kind, display = classify_shell_executable(name)
        if kind == "unknown":
            continue
        try:
            executable = proc.exe()
        except Exception:
            executable = ""
        resolved = _resolve_candidate_path(executable) or _resolve_candidate_path(name) or executable or name
        return {
            "source": "follow_terminal",
            "terminal_kind": kind,
            "display_name": display,
            "executable": resolved,
            "available": bool(resolved),
        }

    return None


def _fallback_follow_shell() -> Dict[str, str]:
    """Build a best-effort shell fallback when ancestry detection is unavailable."""

    if os.name == "nt":
        candidate = str(os.getenv("COMSPEC", "") or "").strip() or "cmd.exe"
        resolved = _resolve_candidate_path(candidate) or candidate
        kind, display = classify_shell_executable(resolved)
        if kind == "unknown":
            kind, display = "cmd", _SHELL_DISPLAY_NAMES["cmd"]
        return {
            "source": "follow_terminal_fallback",
            "terminal_kind": kind,
            "display_name": display,
            "executable": resolved,
            "available": bool(resolved),
        }

    candidate = str(os.getenv("SHELL", "") or "").strip()
    resolved = _resolve_candidate_path(candidate)
    if not resolved:
        for fallback in ("sh", "bash"):
            resolved = _resolve_candidate_path(fallback)
            if resolved:
                break
    kind, display = classify_shell_executable(resolved)
    if kind == "unknown":
        kind, display = "sh", _SHELL_DISPLAY_NAMES["sh"]
    return {
        "source": "follow_terminal_fallback",
        "terminal_kind": kind,
        "display_name": display,
        "executable": resolved,
        "available": bool(resolved),
    }


@lru_cache(maxsize=1)
def get_follow_shell_runtime() -> Dict[str, str]:
    """Return the cached runtime info for the current launching terminal."""

    return _find_shell_in_process_tree() or _fallback_follow_shell()


def get_shell_runtime_for_workspace(settings_root: str | None = None) -> Dict[str, Any]:
    """Return the effective shell runtime configuration for one workspace."""

    mode, configured_shell = _load_shell_preferences(settings_root)
    if mode == SHELL_MODE_CUSTOM:
        raw_executable = configured_shell or str(os.getenv(ENV_KEY_SHELL, "") or "").strip()
        resolved = _resolve_candidate_path(raw_executable) or raw_executable
        kind, display = classify_shell_executable(resolved)
        return {
            "mode": SHELL_MODE_CUSTOM,
            "source": "custom_shell",
            "terminal_kind": kind,
            "display_name": display if kind != "unknown" else "Custom shell",
            "executable": resolved,
            "available": bool(_resolve_candidate_path(raw_executable)),
        }

    runtime = dict(get_follow_shell_runtime())
    runtime["mode"] = SHELL_MODE_FOLLOW
    return runtime


def get_shell_runtime_for_task(task_dir: str | None = None) -> Dict[str, Any]:
    """Return the effective shell runtime configuration for one task directory."""

    return get_shell_runtime_for_workspace(_shell_settings_root_for_task(task_dir))
