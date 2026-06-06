"""Run a single task as a subprocess and persist its lifecycle."""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from pyruns._config import (
    CONFIG_FILENAME,
    DEFAULT_RUNNER_HEARTBEAT_SECONDS,
    DEFAULT_RUNNER_LEASE_SECONDS,
    DEFAULT_ROOT_NAME,
    ENV_KEY_CLI_TERMINAL_RUNTIME,
    ENV_KEY_CONDA_ENV,
    ENV_KEY_CONDA_EXE,
    ENV_KEY_CONFIG,
    ENV_KEY_PYTHON_EXECUTABLE,
    ENV_KEY_RUN_INDEX,
    ERROR_LOG_FILENAME,
    RECORDS_KEY,
    RUN_LOGS_DIR,
    SCRIPT_INFO_FILENAME,
    SHELL_CONFIG_FILENAME,
    SHELL_WORKSPACE_NAME,
    TASK_KIND_CONFIG,
    TASK_KIND_SHELL,
    TRACKS_KEY,
)
from pyruns.utils import get_logger, get_now_str
from pyruns.utils.events import log_emitter
from pyruns.utils.info_io import (
    ensure_run_slot,
    load_task_info,
    update_task_info,
)
from pyruns.utils.log_io import normalize_log_newlines
from pyruns.utils.shell_runtime import get_shell_runtime_for_task
from pyruns.utils.settings import load_settings
from pyruns.utils.task_files import normalize_task_kind, resolve_task_config_file

logger = get_logger(__name__)
_ISOLATED_IMPORT_ROOT_LOCK = threading.Lock()
_ISOLATED_IMPORT_ROOT_CACHE: Dict[str, str] = {}
_SITE_GUARD_ROOT_CACHE: Dict[str, str] = {}
_SOURCE_STATE_GIT_TIMEOUT_SEC = 1.0
_SOURCE_STATE_DIGEST_LEN = 12


def _lifecycle_banner(phase: str, name: str, timestamp: str) -> str:
    """Build a more visible lifecycle banner for task start/finish logs."""

    title = phase.upper()
    return (
        f"[PYRUNS] {'=' * 20} {title} {'=' * 20}\n"
        f"[PYRUNS] Task {name} {phase.lower()}ed at {timestamp}\n"
        f"[PYRUNS] {'=' * (42 + len(title))}\n"
    )


def _append_run_log_text(log_path: str, text: str, *, clean_boundary: bool = False) -> str:
    payload = text
    if clean_boundary and text:
        try:
            if os.path.getsize(log_path) > 0:
                with open(log_path, "rb") as handle:
                    handle.seek(-1, os.SEEK_END)
                    if handle.read(1) != b"\n":
                        payload = "\n" + text
        except OSError:
            pass

    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(payload)
    return payload


def _prepend_pythonpath(env: Dict[str, str], path: str) -> None:
    """Ensure child scripts can import the same pyruns package as the parent."""

    if not path or not os.path.isdir(path):
        return

    existing = str(env.get("PYTHONPATH", "") or "")
    entries = [entry for entry in existing.split(os.pathsep) if entry]
    normalized_entries = {
        os.path.normcase(os.path.abspath(entry))
        for entry in entries
    }
    normalized_path = os.path.normcase(os.path.abspath(path))
    if normalized_path in normalized_entries:
        return

    env["PYTHONPATH"] = path if not existing else f"{path}{os.pathsep}{existing}"


def _copy_ignore(_: str, names: List[str]) -> set[str]:
    return {
        name
        for name in names
        if name == "__pycache__" or name == ".pytest_cache" or name.endswith(".pyc")
    }


def _copy_dist_info(package_parent: str, import_root: str) -> None:
    try:
        entries = os.listdir(package_parent)
    except OSError:
        return
    for name in entries:
        lower_name = name.lower()
        if not lower_name.startswith("pyruns-") or not lower_name.endswith(".dist-info"):
            continue
        source = os.path.join(package_parent, name)
        target = os.path.join(import_root, name)
        if not os.path.isdir(source) or os.path.exists(target):
            continue
        try:
            shutil.copytree(source, target, ignore=_copy_ignore)
        except OSError:
            logger.debug("Skipping pyruns dist-info copy for %s", source, exc_info=True)


def _pyruns_package_fingerprint(package_dir: str) -> str:
    """Fingerprint Python-loadable package files while ignoring bulky static assets."""

    package_dir = os.path.abspath(package_dir)
    hasher = hashlib.sha1(package_dir.encode("utf-8"))
    suffixes = (".py", ".pyw", ".pyi", ".pyd", ".so", ".dll", ".dylib")
    ignored_dirs = {"__pycache__", ".pytest_cache", "static"}
    try:
        walker = os.walk(package_dir)
        for dirpath, dirnames, filenames in walker:
            dirnames[:] = sorted(name for name in dirnames if name not in ignored_dirs)
            for filename in sorted(filenames):
                if not filename.endswith(suffixes):
                    continue
                path = os.path.join(dirpath, filename)
                relative_path = os.path.relpath(path, package_dir).replace(os.sep, "/")
                try:
                    stat_result = os.stat(path)
                except OSError:
                    hasher.update(f"{relative_path}:missing".encode("utf-8"))
                    continue
                hasher.update(
                    f"{relative_path}:{stat_result.st_mtime_ns}:{stat_result.st_size}".encode("utf-8")
                )
    except OSError:
        hasher.update(b":walk-error")
    return hasher.hexdigest()


def _isolated_pyruns_import_root(package_dir: str) -> str:
    """Expose only the current pyruns package, not its parent import root."""

    package_dir = os.path.abspath(package_dir)
    package_parent = os.path.dirname(package_dir)
    fingerprint = _pyruns_package_fingerprint(package_dir)
    digest = hashlib.sha1(f"{fingerprint}:{os.getpid()}".encode("utf-8")).hexdigest()[:16]
    import_root = os.path.join(tempfile.gettempdir(), f"pyruns-import-{digest}")
    target_package = os.path.join(import_root, "pyruns")

    with _ISOLATED_IMPORT_ROOT_LOCK:
        cached_root = _ISOLATED_IMPORT_ROOT_CACHE.get(fingerprint)
        if cached_root and os.path.isdir(os.path.join(cached_root, "pyruns")):
            return cached_root
        if os.path.isdir(target_package):
            _ISOLATED_IMPORT_ROOT_CACHE[fingerprint] = import_root
            return import_root
        try:
            os.makedirs(import_root, exist_ok=True)
            shutil.copytree(package_dir, target_package, ignore=_copy_ignore)
            _copy_dist_info(package_parent, import_root)
        except Exception as exc:
            raise RuntimeError("Unable to isolate the current pyruns package for child tasks.") from exc
        _ISOLATED_IMPORT_ROOT_CACHE[fingerprint] = import_root
    return import_root


def _sitecustomize_guard_content(import_root: str) -> str:
    return f"""# Auto-generated by pyruns. Route pyruns imports to the server package before user scripts run.
import importlib.machinery
import importlib.util
import os
import sys

_PYRUNS_IMPORT_ROOT = {import_root!r}
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_THIS_FILE = os.path.abspath(__file__)


def _norm(path):
    try:
        value = path or os.getcwd()
        return os.path.normcase(os.path.abspath(value))
    except Exception:
        return os.path.normcase(str(path or ""))


class _PyrunsImportGuard:
    _pyruns_import_root = _PYRUNS_IMPORT_ROOT

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "pyruns":
            return None
        if not _PYRUNS_IMPORT_ROOT or not os.path.isdir(_PYRUNS_IMPORT_ROOT):
            return None
        return importlib.machinery.PathFinder.find_spec(fullname, [_PYRUNS_IMPORT_ROOT])


def _install_pyruns_guard():
    if not _PYRUNS_IMPORT_ROOT or not os.path.isdir(_PYRUNS_IMPORT_ROOT):
        return
    for finder in sys.meta_path:
        if getattr(finder, "_pyruns_import_root", None) == _PYRUNS_IMPORT_ROOT:
            return
    sys.meta_path.insert(0, _PyrunsImportGuard())


def _chain_user_sitecustomize():
    for entry in list(sys.path):
        if _norm(entry) == _norm(_THIS_DIR):
            continue
        try:
            spec = importlib.machinery.PathFinder.find_spec("sitecustomize", [entry or os.getcwd()])
        except Exception:
            continue
        if not spec or not spec.loader or not spec.origin:
            continue
        if _norm(spec.origin) == _norm(_THIS_FILE):
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        break


_install_pyruns_guard()
_chain_user_sitecustomize()
"""


def _pyruns_sitecustomize_guard_root(import_root: str) -> str:
    """Create a startup guard that preloads pyruns before project-local shadows."""

    import_root = os.path.abspath(import_root)
    digest = hashlib.sha1(f"{import_root}:{os.getpid()}".encode("utf-8")).hexdigest()[:16]
    guard_root = os.path.join(tempfile.gettempdir(), f"pyruns-guard-{digest}")
    guard_path = os.path.join(guard_root, "sitecustomize.py")
    content = _sitecustomize_guard_content(import_root)

    with _ISOLATED_IMPORT_ROOT_LOCK:
        cached_root = _SITE_GUARD_ROOT_CACHE.get(import_root)
        if cached_root and os.path.exists(os.path.join(cached_root, "sitecustomize.py")):
            return cached_root
        os.makedirs(guard_root, exist_ok=True)
        try:
            existing = ""
            if os.path.exists(guard_path):
                with open(guard_path, "r", encoding="utf-8") as handle:
                    existing = handle.read()
            if existing != content:
                with open(guard_path, "w", encoding="utf-8") as handle:
                    handle.write(content)
        except OSError as exc:
            raise RuntimeError("Unable to prepare pyruns import guard for child tasks.") from exc
        _SITE_GUARD_ROOT_CACHE[import_root] = guard_root
    return guard_root


def _current_pyruns_import_root() -> str:
    package_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    return _isolated_pyruns_import_root(package_dir)


def _is_windows() -> bool:
    return os.name == "nt"


def _popen_process_group_kwargs() -> Dict[str, Any]:
    """Return subprocess options that let Pyruns stop a full POSIX task tree."""

    if _is_windows():
        return {}
    return {"start_new_session": True}


def _path_env_key(env: Dict[str, str]) -> str:
    """Return the existing PATH key while preserving platform spelling."""

    for key in ("PATH", "Path", "path"):
        if key in env:
            return key
    for key in env:
        if key.upper() == "PATH":
            return key
    return "PATH"


def _prepend_path_entries(env: Dict[str, str], paths: List[str]) -> None:
    """Move valid path entries to the front of PATH without duplicates."""

    key = _path_env_key(env)
    existing = str(env.get(key, "") or "")
    entries = [entry for entry in existing.split(os.pathsep) if entry]
    normalized_front: set[str] = set()
    front: List[str] = []
    for path in paths:
        if not path or not os.path.isdir(path):
            continue
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in normalized_front:
            continue
        normalized_front.add(normalized)
        front.append(path)

    if not front:
        return

    retained: List[str] = []
    seen = set(normalized_front)
    for entry in entries:
        normalized = os.path.normcase(os.path.abspath(entry))
        if normalized in seen:
            continue
        seen.add(normalized)
        retained.append(entry)

    env[key] = os.pathsep.join(front + retained)
    for duplicate_key in list(env):
        if duplicate_key != key and duplicate_key.upper() == "PATH":
            env.pop(duplicate_key, None)


def _prepend_current_python_to_path(env: Dict[str, str]) -> None:
    """Make shell tasks resolve ``python`` to the interpreter running pyruns."""

    executable_dir = os.path.dirname(sys.executable)
    candidates = [executable_dir]
    if _is_windows():
        candidates.append(os.path.join(executable_dir, "Scripts"))
    _prepend_path_entries(env, candidates)


def _python_runtime_settings_root(task_dir: str | None = None) -> str | None:
    if not task_dir:
        return None
    return os.path.dirname(os.path.dirname(os.path.abspath(task_dir)))


def _clean_runtime_value(value: Any) -> str:
    return str(value or "").strip()


def _cli_terminal_runtime_enabled() -> bool:
    return str(os.getenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "")).strip().lower() in {"1", "true", "yes", "on"}


def _resolve_executable_path(candidate: str) -> str:
    raw = os.path.expanduser(os.path.expandvars(_clean_runtime_value(candidate)))
    if not raw:
        return ""
    if os.path.isabs(raw) or os.path.dirname(raw):
        return os.path.abspath(raw) if os.path.exists(raw) else ""
    resolved = shutil.which(raw)
    return os.path.abspath(resolved) if resolved else ""


def _runtime_from_values(
    *,
    python_executable: Any = None,
    conda_env: Any = None,
    conda_executable: Any = None,
    source: str,
) -> Dict[str, str] | None:
    python_raw = _clean_runtime_value(python_executable)
    conda_raw = _clean_runtime_value(conda_env)
    conda_exe_raw = _clean_runtime_value(conda_executable) or "conda"

    if python_raw:
        resolved_python = _resolve_executable_path(python_raw)
        if not resolved_python:
            raise RuntimeError(f"python_executable is set but not found: {python_raw}")
        return {
            "mode": "python",
            "source": source,
            "python_executable": resolved_python,
        }

    if conda_raw:
        resolved_conda = _resolve_executable_path(conda_exe_raw)
        if not resolved_conda:
            raise RuntimeError(f"conda_env is set but conda_executable was not found: {conda_exe_raw}")
        return {
            "mode": "conda",
            "source": source,
            "conda_env": conda_raw,
            "conda_executable": resolved_conda,
        }

    return None


def _resolve_python_runtime(
    task_dir: str | None = None,
    extra_env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Resolve the Python runtime used by Python tasks and shell task PATH."""

    extra_env = extra_env or {}
    task_runtime = _runtime_from_values(
        python_executable=extra_env.get(ENV_KEY_PYTHON_EXECUTABLE),
        conda_env=extra_env.get(ENV_KEY_CONDA_ENV),
        conda_executable=extra_env.get(ENV_KEY_CONDA_EXE),
        source="task_env",
    )
    if task_runtime:
        return task_runtime

    if not _cli_terminal_runtime_enabled():
        settings_root = _python_runtime_settings_root(task_dir)
        settings = load_settings(settings_root) if settings_root else load_settings()
        settings_runtime = _runtime_from_values(
            python_executable=settings.get("python_executable"),
            conda_env=settings.get("conda_env"),
            conda_executable=settings.get("conda_executable"),
            source="workspace_settings",
        )
        if settings_runtime:
            return settings_runtime

    process_runtime = _runtime_from_values(
        python_executable=os.getenv(ENV_KEY_PYTHON_EXECUTABLE),
        conda_env=os.getenv(ENV_KEY_CONDA_ENV),
        conda_executable=os.getenv(ENV_KEY_CONDA_EXE) or os.getenv("CONDA_EXE"),
        source="process_env",
    )
    if process_runtime:
        return process_runtime

    return {
        "mode": "follow",
        "source": "pyruns_process",
        "python_executable": sys.executable,
    }


def _load_workspace_global_env(task_dir: str | None = None) -> Dict[str, str]:
    if _cli_terminal_runtime_enabled():
        return {}
    settings_root = _python_runtime_settings_root(task_dir)
    settings = load_settings(settings_root) if settings_root else load_settings()
    raw_env = settings.get("global_env", {})
    if not isinstance(raw_env, dict):
        return {}
    return {str(k): str(v) for k, v in raw_env.items() if k and v is not None}


def _python_command_prefix(python_runtime: Dict[str, str] | None = None) -> List[str]:
    runtime = python_runtime or {"mode": "follow", "python_executable": sys.executable}
    mode = runtime.get("mode")
    if mode == "python":
        return [runtime["python_executable"]]
    if mode == "conda":
        return [
            runtime["conda_executable"],
            "run",
            "-n",
            runtime["conda_env"],
            "--no-capture-output",
            "python",
        ]
    return [sys.executable]


def _apply_python_runtime_to_shell_command(
    command: List[str],
    python_runtime: Dict[str, str] | None = None,
) -> List[str]:
    runtime = python_runtime or {"mode": "follow"}
    if runtime.get("mode") != "conda":
        return command
    return [
        runtime["conda_executable"],
        "run",
        "-n",
        runtime["conda_env"],
        "--no-capture-output",
        *command,
    ]


def _prepend_runtime_python_to_path(env: Dict[str, str], python_runtime: Dict[str, str] | None = None) -> None:
    runtime = python_runtime or {"mode": "follow", "python_executable": sys.executable}
    mode = runtime.get("mode")
    if mode == "conda":
        env[ENV_KEY_CONDA_ENV] = runtime["conda_env"]
        env[ENV_KEY_CONDA_EXE] = runtime["conda_executable"]
        return

    executable = runtime.get("python_executable") or sys.executable
    executable_dir = os.path.dirname(executable)
    candidates = [executable_dir]
    if _is_windows():
        candidates.append(os.path.join(executable_dir, "Scripts"))
    _prepend_path_entries(env, candidates)
    env[ENV_KEY_PYTHON_EXECUTABLE] = executable


def _prepare_env(
    extra_env: Optional[Dict[str, str]] = None,
    *,
    task_dir: Optional[str] = None,
    task_kind: str = TASK_KIND_CONFIG,
    config_file: str = CONFIG_FILENAME,
    python_runtime: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Build the subprocess environment."""

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if task_dir and normalize_task_kind(task_kind) == TASK_KIND_CONFIG:
        env[ENV_KEY_CONFIG] = os.path.join(task_dir, config_file or CONFIG_FILENAME)
    else:
        env.pop(ENV_KEY_CONFIG, None)
    env.update(_load_workspace_global_env(task_dir))
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items() if k})
    _prepend_runtime_python_to_path(env, python_runtime)
    pyruns_import_root = _current_pyruns_import_root()
    _prepend_pythonpath(env, pyruns_import_root)
    _prepend_pythonpath(env, _pyruns_sitecustomize_guard_root(pyruns_import_root))
    return env


def _get_log_path(task_dir: str, run_index: int) -> str:
    """Return ``run_logs/runN.log`` and create the directory when needed."""

    log_dir = os.path.join(task_dir, RUN_LOGS_DIR)
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"run{run_index}.log")


def _file_sha256(path: str | None) -> str:
    if not path:
        return "none"
    try:
        hasher = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()[:_SOURCE_STATE_DIGEST_LEN]
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "error"


def _git_bytes(cwd: str, args: List[str], *, timeout: float = _SOURCE_STATE_GIT_TIMEOUT_SEC) -> bytes | None:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _build_git_source_state(cwd: str) -> str:
    git_root_raw = _git_bytes(cwd, ["rev-parse", "--show-toplevel"])
    if not git_root_raw:
        return "git none | unknown"
    git_root = git_root_raw.decode("utf-8", errors="replace").strip()
    head = (_git_bytes(git_root, ["rev-parse", "--short=12", "HEAD"]) or b"").decode(
        "utf-8",
        errors="replace",
    ).strip() or "unknown"

    status_bytes = _git_bytes(git_root, ["status", "--porcelain=v1", "-z", "--untracked-files=normal"])
    if status_bytes is None:
        source_status = "unknown"
    elif status_bytes:
        source_status = "dirty"
    else:
        source_status = "clean"

    return f"git {head} | {source_status}"


def _append_run_slot_value(info: Dict[str, Any], key: str, slot: int, value: Any) -> None:
    values = list(info.get(key, []) or [])
    while len(values) <= slot:
        values.append("")
    values[slot] = value
    info[key] = values


def _set_runner_lease(
    info: Dict[str, Any],
    *,
    runner_id: str,
    runner_host: str,
    lease_seconds: int,
) -> None:
    if not runner_id:
        return
    info["runner_id"] = runner_id
    if runner_host:
        info["runner_host"] = runner_host
    now = time.time()
    info["lease_heartbeat"] = now
    info["lease_until"] = now + max(1, int(lease_seconds or DEFAULT_RUNNER_LEASE_SECONDS))


def _clear_runner_lease(info: Dict[str, Any], runner_id: str) -> None:
    if runner_id and info.get("runner_id") not in {None, "", runner_id}:
        return
    for key in ("runner_id", "runner_host", "lease_heartbeat", "lease_until"):
        info.pop(key, None)


def _build_run_source_state(
    *,
    task_dir: str,
    script_path: str | None,
    workdir: str | None,
) -> str:
    script_abs = os.path.abspath(script_path) if script_path and os.path.exists(script_path) else None
    git_cwd = script_abs and os.path.dirname(script_abs)
    if not git_cwd and workdir and os.path.isdir(workdir):
        git_cwd = workdir

    git_state = _build_git_source_state(git_cwd) if git_cwd else "git none | unknown"
    return " ".join(
        [
            git_state,
            f"| script {_file_sha256(script_abs)}",
        ]
    )


def _persist_run_source_state(
    *,
    task_dir: str,
    task_name: str,
    log_path: str,
    run_index: int,
    source_state: str,
) -> None:
    if not source_state:
        return

    line = f"[PYRUNS] Source {source_state}\n"
    try:
        payload = _append_run_log_text(log_path, line, clean_boundary=True)
        log_emitter.emit(task_name, payload.replace("\n", "\r\n"))
    except Exception as exc:
        logger.debug("Failed to append source state log for %s: %s", task_name, exc)

    def _apply(info: Dict[str, Any]) -> None:
        slot = ensure_run_slot(info, run_index)
        _append_run_slot_value(info, "source_states", slot, source_state)

    try:
        update_task_info(task_dir, _apply)
    except Exception as exc:
        logger.debug("Failed to persist source state for %s: %s", task_name, exc)


def _resolve_shell_executable(task_dir: str | None = None) -> str:
    """Resolve the shell executable used for shell tasks."""
    runtime = get_shell_runtime_for_task(task_dir)
    shell_path = str(runtime.get("executable", "") or "").strip()
    if shell_path and bool(runtime.get("available", False)):
        return shell_path

    if runtime.get("mode") == "custom":
        raise RuntimeError(
            "shell_mode=custom requires a valid shell_executable in "
            "_pyruns_settings.yaml before running shell tasks."
        )

    raise RuntimeError(
        "Unable to resolve the current terminal shell for shell tasks. "
        "Start pyr from the shell you want to follow, or switch to "
        "shell_mode=custom in _pyruns_settings.yaml."
    )


def _is_posix_shell_executable(shell_path: str) -> bool:
    """Return True when the resolved shell should execute the script directly."""

    name = os.path.basename(shell_path).lower()
    return name in {"bash", "bash.exe", "sh", "sh.exe", "zsh", "zsh.exe", "fish", "fish.exe"}


def _is_powershell_executable(shell_path: str) -> bool:
    """Return True when the resolved shell uses PowerShell script semantics."""

    name = os.path.basename(shell_path).lower()
    return "powershell" in name or name.startswith("pwsh")


def _read_shell_script_body(script_path: str) -> str:
    """Read the stored shell script and strip any shebang for native wrappers."""

    with open(script_path, "r", encoding="utf-8") as handle:
        body = handle.read()

    if body.startswith("#!"):
        _, _, remainder = body.partition("\n")
        body = remainder
    return body.lstrip("\r\n")


def _powershell_utf8_preamble() -> str:
    """Force PowerShell wrapper I/O through UTF-8 for stable log capture."""

    return "\n".join(
        [
            "$__pyrunsUtf8 = [System.Text.UTF8Encoding]::new($false)",
            "[Console]::InputEncoding = $__pyrunsUtf8",
            "[Console]::OutputEncoding = $__pyrunsUtf8",
            "$OutputEncoding = $__pyrunsUtf8",
        ]
    )


def _write_temp_shell_wrapper(
    *,
    suffix: str,
    content: str,
    encoding: str,
    newline: str,
) -> str:
    """Write one temporary wrapper file outside the task directory."""

    fd, wrapper_path = tempfile.mkstemp(prefix="pyruns-shell-", suffix=suffix)
    os.close(fd)
    with open(wrapper_path, "w", encoding=encoding, newline=newline) as handle:
        handle.write(content)
    return wrapper_path


def _normalize_execution_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(str(path)))).replace("\\", "/")


def _resolve_shell_workdir(task_dir: str) -> str:
    """Return the project directory where shell payloads should execute."""

    workspace_dir = os.path.dirname(os.path.dirname(task_dir))
    script_info_path = os.path.join(workspace_dir, SCRIPT_INFO_FILENAME)
    if os.path.exists(script_info_path):
        try:
            with open(script_info_path, "r", encoding="utf-8") as handle:
                info = json.load(handle)
            project_root = str(info.get("project_root", "") or "").strip()
            if project_root:
                resolved = _normalize_execution_path(project_root)
                if os.path.isdir(resolved):
                    return resolved
        except Exception as exc:
            logger.warning("Failed to read shell workspace project root from %s: %s", script_info_path, exc)

    parent_root = os.path.dirname(workspace_dir)
    if os.path.basename(workspace_dir) == SHELL_WORKSPACE_NAME and os.path.basename(parent_root) == DEFAULT_ROOT_NAME:
        project_root = _normalize_execution_path(os.path.dirname(parent_root))
        if os.path.isdir(project_root):
            return project_root

    return task_dir


def _materialize_windows_shell_wrapper(
    task_dir: str,
    script_path: str,
    shell_path: str,
) -> Tuple[List[str], str, List[str]]:
    """Create a native Windows wrapper script around the stored shell task body."""

    if _is_posix_shell_executable(shell_path):
        return [shell_path, script_path], task_dir, []

    script_body = _read_shell_script_body(script_path)
    if _is_powershell_executable(shell_path):
        wrapper_body = "\n".join(
            [
                _powershell_utf8_preamble(),
                script_body.rstrip() or "exit 0",
            ]
        )
        wrapper_path = _write_temp_shell_wrapper(
            suffix=".ps1",
            content=wrapper_body,
            encoding="utf-8-sig",
            newline="\r\n",
        )
        return [
            shell_path,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            wrapper_path,
        ], task_dir, [wrapper_path]

    wrapper_lines = [
        "@echo off",
        "setlocal",
        "chcp 65001 >nul",
        script_body.rstrip() or "exit /b 0",
    ]
    wrapper_path = _write_temp_shell_wrapper(
        suffix=".cmd",
        content="\r\n".join(wrapper_lines) + "\r\n",
        encoding="utf-8-sig",
        newline="\r\n",
    )
    return [shell_path, "/d", "/c", wrapper_path], task_dir, [wrapper_path]


def _build_shell_command(task_dir: str, config_file: str) -> Tuple[List[str], str, List[str]]:
    script_path = os.path.join(task_dir, config_file or SHELL_CONFIG_FILENAME)
    if not os.path.exists(script_path):
        raise FileNotFoundError(script_path)
    shell_path = _resolve_shell_executable(task_dir)
    workdir = _resolve_shell_workdir(task_dir)
    if _is_windows():
        command, _, cleanup_paths = _materialize_windows_shell_wrapper(task_dir, script_path, shell_path)
        return command, workdir, cleanup_paths
    return [shell_path, script_path], workdir, []


def _build_command(
    meta_cmd,
    script_path,
    meta_workdir,
    config,
    *,
    task_kind: str = TASK_KIND_CONFIG,
    task_dir: str | None = None,
    config_file: str = CONFIG_FILENAME,
    python_runtime: Optional[Dict[str, str]] = None,
) -> Tuple[Any, Optional[str], List[str]]:
    """Build the subprocess command list from task metadata + payload."""

    normalized_kind = normalize_task_kind(task_kind)
    if normalized_kind == TASK_KIND_SHELL:
        if not task_dir:
            raise RuntimeError("Shell task execution requires a task directory")
        command, workdir, cleanup_paths = _build_shell_command(task_dir, config_file or SHELL_CONFIG_FILENAME)
        return _apply_python_runtime_to_shell_command(command, python_runtime), workdir, cleanup_paths

    command = meta_cmd or config.get("command")
    workdir = meta_workdir

    if not command and script_path:
        cmd_list: List[str] = [*_python_command_prefix(python_runtime), script_path]

        from pyruns.utils.parse_utils import detect_config_source_fast, extract_argparse_params

        config_source, _ = detect_config_source_fast(script_path)

        if config_source == "argparse":
            positional_order: List[str] = []
            ap_params: Dict[str, Dict[str, Any]] = {}
            try:
                ap_params = extract_argparse_params(script_path)
                for dest, info in ap_params.items():
                    raw_name = info.get("name", "")
                    if isinstance(raw_name, (list, tuple)):
                        raw_name = raw_name[0] if raw_name else ""
                    if not str(raw_name).startswith("-"):
                        positional_order.append(dest)
            except Exception:
                pass

            def _option_flag_for_key(key: str) -> str:
                info = ap_params.get(key, {}) if isinstance(ap_params, dict) else {}
                flags = info.get("flags")
                if not isinstance(flags, (list, tuple)):
                    raw_name = info.get("name")
                    flags = [raw_name] if raw_name else []
                normalized_flags = [str(flag) for flag in flags if str(flag).startswith("-")]
                long_flags = [flag for flag in normalized_flags if flag.startswith("--")]
                if long_flags:
                    return long_flags[0]
                if normalized_flags:
                    return normalized_flags[0]
                return f"--{key}"

            def _append_option(key: str, value: Any) -> None:
                info = ap_params.get(key, {}) if isinstance(ap_params, dict) else {}
                action = str(info.get("action", "") or "")
                nargs = info.get("nargs")
                flag = _option_flag_for_key(key)

                def _negative_bool_flag(raw_flag: str) -> str:
                    if raw_flag.startswith("--no-"):
                        return raw_flag
                    if raw_flag.startswith("--"):
                        return f"--no-{raw_flag[2:]}"
                    return raw_flag

                if isinstance(value, bool):
                    if action.endswith("BooleanOptionalAction"):
                        cmd_list.append(flag if value else _negative_bool_flag(flag))
                    elif action == "store_false":
                        if not value:
                            cmd_list.append(flag)
                    elif action == "store_true":
                        if value:
                            cmd_list.append(flag)
                    elif key in ap_params:
                        cmd_list.append(flag)
                        cmd_list.append(str(value))
                    elif value:
                        cmd_list.append(flag)
                    return

                if isinstance(value, list):
                    if action == "append":
                        for item in value:
                            cmd_list.append(flag)
                            if isinstance(item, (list, tuple)) and nargs not in (None, ""):
                                cmd_list.extend(str(part) for part in item)
                            else:
                                cmd_list.append(str(item))
                    elif nargs not in (None, ""):
                        cmd_list.append(flag)
                        cmd_list.extend(str(item) for item in value)
                    else:
                        for item in value:
                            cmd_list.append(flag)
                            cmd_list.append(str(item))
                    return

                if action == "count":
                    try:
                        repeat = int(value)
                    except (TypeError, ValueError):
                        repeat = 0
                    if repeat > 0:
                        cmd_list.extend([flag] * repeat)
                    return

                if value is not None:
                    cmd_list.append(flag)
                    cmd_list.append(str(value))

            for key in positional_order:
                if key in config and config[key] is not None:
                    value = config[key]
                    if isinstance(value, list):
                        for item in value:
                            cmd_list.append(str(item))
                    else:
                        cmd_list.append(str(value))

            for key, value in config.items():
                if key in positional_order:
                    continue
                _append_option(key, value)
        elif config_source == "hydra":
            raise RuntimeError(
                "Hydra script detected. Use a shell workspace/task to launch it explicitly."
            )
        elif config_source == "unknown":
            raise RuntimeError(
                "Unable to detect script configuration style safely. Use a shell workspace/task, "
                "or integrate via argparse / pyruns.load()."
            )

        command = cmd_list
        if not workdir:
            workdir = os.path.dirname(script_path)

    return command, workdir, []


def _append_error_summary(
    task_dir: str,
    *,
    run_index: int,
    title: str,
    detail_lines: List[str],
) -> None:
    """Append one failure/error summary block into ``error.log``."""

    err_log_path = os.path.join(task_dir, RUN_LOGS_DIR, ERROR_LOG_FILENAME)
    os.makedirs(os.path.dirname(err_log_path), exist_ok=True)
    block = (
        f"\n\n{'=' * 70}\n"
        f"[PYRUNS] {title}\n"
        + "\n".join(detail_lines)
        + f"\n{'=' * 70}\n"
    )
    with open(err_log_path, "a", encoding="utf-8") as handle:
        handle.write(block)


def _consume_pending_stop_summary(task_dir: str, run_index: int) -> Dict[str, Any] | None:
    """Pop one pending stop summary for the finished run, if present."""

    captured: Dict[str, Any] = {}

    def _apply(info: Dict[str, Any]) -> None:
        raw = info.get("_pending_stop_summary")
        if not isinstance(raw, dict):
            return
        raw_index = int(raw.get("run_index", 0) or 0)
        if raw_index != int(run_index):
            return
        captured.update(raw)
        info.pop("_pending_stop_summary", None)

    update_task_info(task_dir, _apply)
    return captured or None


def run_task_worker(
    task_dir: str,
    name: str,
    created_at: str,
    config: Dict[str, Any],
    env_vars: Optional[Dict[str, str]] = None,
    run_index: int = 1,
    runner_id: str = "",
    runner_host: str = "",
    lease_seconds: int = DEFAULT_RUNNER_LEASE_SECONDS,
) -> Dict[str, Any]:
    """Worker function executed in a separate thread/process."""

    logger.info("Task %s starting  run=#%d", name, run_index)

    log_path = _get_log_path(task_dir, run_index)
    task_meta = load_task_info(task_dir)
    task_kind = normalize_task_kind(task_meta.get("task_kind", task_meta.get("config_mode")))
    config_file = resolve_task_config_file(task_meta, task_kind, task_dir)
    script_path = task_meta.get("script")
    meta_cmd = task_meta.get("cmd")
    meta_workdir = task_meta.get("workdir")

    workspace_dir = os.path.dirname(os.path.dirname(task_dir))
    script_info_path = os.path.join(workspace_dir, "script_info.json")
    if task_kind == TASK_KIND_CONFIG and os.path.exists(script_info_path):
        try:
            with open(script_info_path, "r", encoding="utf-8") as handle:
                s_info = __import__("json").load(handle)
            info_script = s_info.get("script_path")
            if info_script and os.path.exists(info_script):
                script_path = info_script
            elif info_script and not os.path.exists(info_script):
                script_basename = os.path.basename(info_script)
                pyruns_parent = os.path.dirname(os.path.dirname(workspace_dir))
                candidate = os.path.join(pyruns_parent, script_basename)
                if os.path.exists(candidate):
                    script_path = candidate
                    logger.info(
                        "script_info path stale (%s), resolved to %s",
                        info_script,
                        candidate,
                    )
        except Exception as exc:
            logger.warning("Failed to read script_info.json from %s: %s", workspace_dir, exc)

    command = None
    workdir = None
    cleanup_paths: List[str] = []
    proc = None
    status = "failed"
    progress = 0.0
    start_str = ""
    end_str = ""
    heartbeat_stop = threading.Event()
    heartbeat_thread: threading.Thread | None = None

    def _refresh_runner_lease() -> None:
        if not runner_id:
            return

        def _apply(info: Dict[str, Any]) -> None:
            if info.get("runner_id") not in {None, "", runner_id}:
                return
            if info.get("status") == "running":
                _set_runner_lease(
                    info,
                    runner_id=runner_id,
                    runner_host=runner_host,
                    lease_seconds=lease_seconds,
                )

        try:
            update_task_info(task_dir, _apply)
        except Exception as exc:
            logger.debug("Failed to refresh runner lease for %s: %s", name, exc)

    def _heartbeat_loop() -> None:
        interval = max(1, min(DEFAULT_RUNNER_HEARTBEAT_SECONDS, max(1, int(lease_seconds) // 3)))
        while not heartbeat_stop.wait(interval):
            _refresh_runner_lease()

    def _collect_source_state_async() -> None:
        try:
            collected = _build_run_source_state(
                task_dir=task_dir,
                script_path=script_path,
                workdir=workdir,
            )
        except Exception as exc:
            logger.debug("Failed to build source state for %s: %s", name, exc)
            return

        _persist_run_source_state(
            task_dir=task_dir,
            task_name=name,
            log_path=log_path,
            run_index=run_index,
            source_state=collected,
        )

    try:
        python_runtime = _resolve_python_runtime(task_dir, env_vars)
        command, workdir, cleanup_paths = _build_command(
            meta_cmd,
            script_path,
            meta_workdir,
            config,
            task_kind=task_kind,
            task_dir=task_dir,
            config_file=config_file or (
                SHELL_CONFIG_FILENAME if task_kind == TASK_KIND_SHELL else CONFIG_FILENAME
            ),
            python_runtime=python_runtime,
        )
        logger.debug("Built command: %s  workdir=%s  python_runtime=%s", command, workdir, python_runtime)

        if not command:
            raise NotImplementedError("No command to run (simulation mode not implemented)")

        env = _prepare_env(
            env_vars,
            task_dir=task_dir,
            task_kind=task_kind,
            config_file=config_file or CONFIG_FILENAME,
            python_runtime=python_runtime,
        )
        env[ENV_KEY_RUN_INDEX] = str(run_index)

        if workdir and not os.path.isdir(workdir):
            fallback = task_dir if task_kind == TASK_KIND_SHELL else (os.path.dirname(script_path) if script_path else os.getcwd())
            logger.warning(
                "Workdir '%s' is invalid, falling back to '%s'",
                workdir,
                fallback,
            )
            workdir = fallback

        if isinstance(command, str):
            command = shlex.split(command, posix=not _is_windows())

        proc = subprocess.Popen(
            command,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=workdir,
            env=env,
            **_popen_process_group_kwargs(),
        )

        start_str = get_now_str()
        start_log = _lifecycle_banner("start", name, start_str)
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(start_log)
        log_emitter.emit(name, start_log.replace("\n", "\r\n"))

        def _mark_started(info: Dict[str, Any]) -> None:
            slot = ensure_run_slot(info, run_index)
            info["status"] = "running"
            info["progress"] = 0.0
            info["start_times"][slot] = start_str
            info["pids"][slot] = proc.pid
            _set_runner_lease(
                info,
                runner_id=runner_id,
                runner_host=runner_host,
                lease_seconds=lease_seconds,
            )

        update_task_info(task_dir, _mark_started)

        threading.Thread(target=_collect_source_state_async, daemon=True).start()

        if runner_id:
            heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
            heartbeat_thread.start()

        def _tee_output() -> None:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            with open(log_path, "ab") as handle:
                for chunk in iter(lambda: proc.stdout.read1(4096), b""):
                    if not chunk:
                        break
                    handle.write(chunk)
                    handle.flush()
                    text = normalize_log_newlines(decoder.decode(chunk))
                    log_emitter.emit(name, text)
                tail = decoder.decode(b"", final=True)
                if tail:
                    log_emitter.emit(name, normalize_log_newlines(tail))

        reader_thread = threading.Thread(target=_tee_output, daemon=True)
        reader_thread.start()
        ret = proc.wait()
        reader_thread.join(timeout=5)

        status = "completed" if ret == 0 else "failed"
        progress = 1.0 if ret == 0 else 0.0
        end_str = get_now_str()

        finish_log = _lifecycle_banner("finish", name, end_str)
        finish_payload = _append_run_log_text(log_path, finish_log, clean_boundary=True)
        log_emitter.emit(name, finish_payload.replace("\n", "\r\n"))

        def _mark_finished(info: Dict[str, Any]) -> None:
            slot = ensure_run_slot(info, run_index)
            info["status"] = status
            info["progress"] = progress
            if start_str and not info["start_times"][slot]:
                info["start_times"][slot] = start_str
            info["finish_times"][slot] = end_str
            if proc is not None:
                info["pids"][slot] = proc.pid
            if RECORDS_KEY not in info:
                info[RECORDS_KEY] = []
            if TRACKS_KEY not in info:
                info[TRACKS_KEY] = []
            _clear_runner_lease(info, runner_id)

        update_task_info(task_dir, _mark_finished)

        if status == "failed":
            stop_summary = _consume_pending_stop_summary(task_dir, run_index)
            if stop_summary:
                detail_lines = [f"reason={stop_summary.get('reason', 'stopped')}"]
                detail_lines.extend(list(stop_summary.get("detail_lines", []) or []))
                detail_lines.extend(
                    [
                        f"exit_code={ret}",
                        f"command={command!r}",
                        f"workdir={workdir!r}",
                        f"log={log_path}",
                    ]
                )
                _append_error_summary(
                    task_dir,
                    run_index=run_index,
                    title=f"Run #{run_index} {stop_summary.get('event', 'stopped')} at {end_str}",
                    detail_lines=detail_lines,
                )
            else:
                _append_error_summary(
                    task_dir,
                    run_index=run_index,
                    title=f"Run #{run_index} failed at {end_str}",
                    detail_lines=[
                        f"reason=exit_code {ret}",
                        f"command={command!r}",
                        f"workdir={workdir!r}",
                        f"log={log_path}",
                    ],
                )

        logger.info("Task %s finished  status=%s", name, status)
        return {"status": status, "progress": progress}

    except Exception as exc:
        end_str = get_now_str()

        def _mark_error(info: Dict[str, Any]) -> None:
            slot = ensure_run_slot(info, run_index)
            info["status"] = "failed"
            info["progress"] = 0.0
            if start_str and not info["start_times"][slot]:
                info["start_times"][slot] = start_str
            if end_str:
                info["finish_times"][slot] = end_str
            if proc is not None:
                info["pids"][slot] = proc.pid
            _clear_runner_lease(info, runner_id)

        update_task_info(task_dir, _mark_error)

        detail_lines = [
            f"exception={type(exc).__name__}: {exc}",
            f"command={command!r}",
            f"workdir={workdir!r}",
            f"task_dir={task_dir!r}",
        ]
        _append_error_summary(
            task_dir,
            run_index=run_index,
            title=f"Internal error during run #{run_index} at {end_str}",
            detail_lines=detail_lines,
        )

        block = (
            f"\n{'=' * 70}\n"
            f"[PYRUNS ERROR] Task {name} failed\n"
            + "\n".join(detail_lines)
            + f"\n{'=' * 70}"
        )
        logger.error("%s", block)
        return {"status": "failed", "progress": 0.0, "error": str(exc)}
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1)
        for cleanup_path in cleanup_paths:
            try:
                if cleanup_path and os.path.exists(cleanup_path):
                    os.remove(cleanup_path)
            except OSError:
                logger.debug("Failed to remove temporary shell wrapper: %s", cleanup_path)

