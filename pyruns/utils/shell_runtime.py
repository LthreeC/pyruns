"""Shell runtime detection and settings resolution for shell tasks."""

from __future__ import annotations

import os
import ntpath
import shutil
import subprocess
import tempfile
from functools import lru_cache
from typing import Any, Dict

from pyruns._config import (
    ENV_KEY_CLI_SHELL_EXECUTABLE,
    ENV_KEY_SHELL,
    SHELL_CONFIG_FILENAME,
    SHELL_KIND_TO_CONFIG_FILENAME,
)
from pyruns.utils.process_utils import hidden_subprocess_kwargs
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
_CMD_META_CHARS = frozenset("&|<>^()%!")


def quote_windows_cmd_argument(value: str) -> str:
    """Render one literal argv item inside a Windows batch command."""

    value = str(value)
    if "\r" in value or "\n" in value:
        raise ValueError("cmd cannot preserve newlines inside exact command arguments")
    value = value.replace("%", "%%")
    needs_quotes = (
        not value
        or " " in value
        or "\t" in value
        or any(character in _CMD_META_CHARS for character in value)
    )
    if not needs_quotes and '"' not in value:
        return value

    result = ['"'] if needs_quotes else []
    backslashes = 0
    for character in value:
        if character == chr(92):
            backslashes += 1
            continue
        if character == '"':
            result.append(chr(92) * (backslashes * 2 + 1))
            result.append('"')
        else:
            if backslashes:
                result.append(chr(92) * backslashes)
            result.append(character)
        backslashes = 0
    if backslashes:
        result.append(chr(92) * (backslashes * (2 if needs_quotes else 1)))
    if needs_quotes:
        result.append('"')
    return "".join(result)


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


def _is_windows_wsl_bash_executable(executable: str) -> bool:
    """Return True for Windows WSL bash launchers, not Git/MSYS bash."""

    normalized = str(executable or "").strip().replace("/", "\\").lower()
    if not normalized.endswith("\\bash.exe") and normalized != "bash.exe":
        return False
    return (
        normalized.endswith("\\windows\\system32\\bash.exe")
        or normalized.endswith("\\windows\\sysnative\\bash.exe")
        or normalized.endswith("\\microsoft\\windowsapps\\bash.exe")
    )


def _windows_path_to_wsl_path(path: str) -> str:
    drive, tail = ntpath.splitdrive(str(path or ""))
    if len(drive) == 2 and drive[1] == ":":
        normalized_tail = tail.replace("\\", "/").lstrip("/")
        return f"/mnt/{drive[0].lower()}/{normalized_tail}"
    return str(path or "").replace("\\", "/")


def _windows_posix_script_arg(executable: str, script_path: str) -> str:
    """Return the script argument a Windows POSIX shell can open."""

    if _is_windows_wsl_bash_executable(executable):
        return _windows_path_to_wsl_path(script_path)
    return str(script_path or "").replace("\\", "/")


def _probe_windows_posix_script_execution(executable: str) -> bool:
    """Return True when a POSIX shell can execute a Windows-hosted script."""

    fd, script_path = tempfile.mkstemp(prefix="pyruns-shell-probe-", suffix=".sh")
    os.close(fd)
    try:
        with open(script_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("exit 0\n")
        result = subprocess.run(
            [executable, _windows_posix_script_arg(executable, script_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass
    return result.returncode == 0


@lru_cache(maxsize=32)
def _probe_shell_executable(executable: str, kind: str) -> bool:
    """Return True when a resolved shell executable can start a no-op command."""

    if not executable or not os.path.exists(executable):
        return False

    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind == "powershell":
        command = [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "exit 0",
        ]
    elif normalized_kind == "cmd":
        command = [executable, "/d", "/c", "exit", "/b", "0"]
    elif normalized_kind in {"bash", "sh", "zsh", "fish"}:
        if os.name == "nt":
            return _probe_windows_posix_script_execution(executable)
        command = [executable, "-c", "exit 0"]
    else:
        return False

    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


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

    explicit = str(os.getenv(ENV_KEY_CLI_SHELL_EXECUTABLE, "") or "").strip()
    if explicit:
        resolved = _resolve_candidate_path(explicit) or explicit
        kind, display = classify_shell_executable(resolved)
        return {
            "source": "cli_parent_terminal",
            "terminal_kind": kind,
            "display_name": display,
            "executable": resolved,
            "available": bool(_resolve_candidate_path(explicit)),
        }
    return _find_shell_in_process_tree() or _fallback_follow_shell()


def get_shell_runtime_for_workspace(settings_root: str | None = None) -> Dict[str, Any]:
    """Return the effective shell runtime configuration for one workspace."""

    mode, configured_shell = _load_shell_preferences(settings_root)
    if mode == SHELL_MODE_CUSTOM:
        raw_executable = configured_shell or str(os.getenv(ENV_KEY_SHELL, "") or "").strip()
        resolved_path = _resolve_candidate_path(raw_executable)
        resolved = resolved_path or raw_executable
        kind, display = classify_shell_executable(resolved)
        available = bool(resolved_path and _probe_shell_executable(resolved_path, kind))
        return {
            "mode": SHELL_MODE_CUSTOM,
            "source": "custom_shell",
            "terminal_kind": kind,
            "display_name": display if kind != "unknown" else "Custom shell",
            "executable": resolved,
            "available": available,
        }

    runtime = dict(get_follow_shell_runtime())
    raw_executable = str(runtime.get("executable", "") or "").strip()
    resolved_path = _resolve_candidate_path(raw_executable)
    resolved = resolved_path or raw_executable
    kind = str(runtime.get("terminal_kind", "") or "").strip().lower()
    if not kind or kind == "unknown":
        kind, display = classify_shell_executable(resolved)
        runtime["display_name"] = display
    runtime["terminal_kind"] = kind or "unknown"
    runtime["executable"] = resolved
    runtime["available"] = bool(
        resolved_path and _probe_shell_executable(resolved_path, runtime["terminal_kind"])
    )
    runtime["mode"] = SHELL_MODE_FOLLOW
    return runtime


def get_shell_runtime_for_task(task_dir: str | None = None) -> Dict[str, Any]:
    """Return the effective shell runtime configuration for one task directory."""

    return get_shell_runtime_for_workspace(_shell_settings_root_for_task(task_dir))


def _resolve_available_shell(candidates: list[str], kind: str) -> str:
    """Return the first runnable shell candidate of the requested kind."""

    for candidate in candidates:
        resolved = _resolve_candidate_path(candidate)
        if resolved and _probe_shell_executable(resolved, kind):
            return resolved
    return ""


def build_script_file_argv(
    script_path: str,
    script_args: list[str],
    settings_root: str | None = None,
) -> list[str]:
    """Build an explicit interpreter argv for a supported shell script file."""

    extension = os.path.splitext(script_path)[1].lower()
    runtime = get_shell_runtime_for_workspace(settings_root)
    runtime_kind = str(runtime.get("terminal_kind", "") or "").strip().lower()
    runtime_executable = str(runtime.get("executable", "") or "").strip()
    runtime_available = bool(runtime.get("available", False))

    if extension == ".sh":
        executable = (
            runtime_executable
            if runtime_available and runtime_kind in {"bash", "sh"}
            else _resolve_available_shell(["bash"], "bash")
            or _resolve_available_shell(["sh"], "sh")
        )
        if not executable:
            raise RuntimeError(".sh scripts require an available Bash or sh executable")
        executable_script = (
            _windows_posix_script_arg(executable, script_path)
            if os.name == "nt"
            else script_path
        )
        if os.name == "nt" and _is_windows_wsl_bash_executable(executable):
            wsl_executable = _resolve_candidate_path("wsl.exe")
            if not wsl_executable:
                raise RuntimeError("WSL Bash scripts require an available wsl.exe")
            return [
                wsl_executable,
                "--exec",
                "/bin/bash",
                executable_script,
                *script_args,
            ]
        return [executable, executable_script, *script_args]

    if extension == ".ps1":
        executable = (
            runtime_executable
            if runtime_available and runtime_kind == "powershell"
            else _resolve_available_shell(["pwsh", "powershell"], "powershell")
        )
        if not executable:
            raise RuntimeError(".ps1 scripts require an available PowerShell executable")
        command = [executable, "-NoLogo", "-NoProfile", "-NonInteractive"]
        if os.name == "nt":
            command.extend(["-ExecutionPolicy", "Bypass"])
        return [*command, "-File", script_path, *script_args]

    if extension in {".cmd", ".bat"}:
        if os.name != "nt":
            raise RuntimeError(f"{extension} scripts can only run on Windows")
        executable = _resolve_available_shell(
            [str(os.getenv("COMSPEC", "") or ""), "cmd.exe"],
            "cmd",
        )
        if not executable:
            raise RuntimeError(f"{extension} scripts require an available cmd.exe")
        values = [script_path, *script_args]
        if any("\r" in value or "\n" in value for value in values):
            raise RuntimeError("cmd script arguments cannot contain newlines")
        return [
            executable,
            "/d",
            "/s",
            "/v:off",
            "/c",
            script_path,
            *script_args,
        ]

    raise ValueError(f"unsupported shell script type: {extension or '<none>'}")


def get_shell_config_filename_for_workspace(settings_root: str | None = None) -> str:
    """Return the on-disk shell payload filename for one workspace."""

    runtime = get_shell_runtime_for_workspace(settings_root)
    terminal_kind = str(runtime.get("terminal_kind", "") or "").strip().lower()
    return SHELL_KIND_TO_CONFIG_FILENAME.get(terminal_kind, SHELL_CONFIG_FILENAME)


def get_shell_config_filename_for_task(task_dir: str | None = None) -> str:
    """Return the on-disk shell payload filename for one task directory."""

    return get_shell_config_filename_for_workspace(_shell_settings_root_for_task(task_dir))
