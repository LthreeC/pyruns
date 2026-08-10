"""
Workspace settings loader/saver for Pyruns.

Settings are persisted to ``_pyruns_settings.yaml`` under the workspace root.
"""

import json
import math
import os
import re
import secrets
import socket
import tempfile
import threading
import time
from typing import Any, Dict

import yaml

from pyruns.utils.process_utils import get_process_create_time, is_pid_running
from pyruns.utils.info_io import validate_workspace_file

from pyruns._config import (
    SETTINGS_FILENAME,
    ROOT_DIR,
    DEFAULT_ROOT_NAME,
    DEFAULT_UI_PORT,
    DEFAULT_HEADER_REFRESH_INTERVAL,
    DEFAULT_MONITOR_CHUNK_SIZE,
    DEFAULT_MONITOR_LINE_HEIGHT,
    DEFAULT_MONITOR_SCROLLBACK,
    DEFAULT_MONITOR_SIDEBAR_WIDTH_PCT,
    DEFAULT_SHELL_MODE,
)


SETTINGS_DEFAULTS: Dict[str, Any] = {
    # Server
    "ui_port": DEFAULT_UI_PORT,
    # Header
    "header_refresh_interval": DEFAULT_HEADER_REFRESH_INTERVAL,
    # Monitor
    "monitor_chunk_size": DEFAULT_MONITOR_CHUNK_SIZE,
    "monitor_scrollback": DEFAULT_MONITOR_SCROLLBACK,
    "monitor_line_height": DEFAULT_MONITOR_LINE_HEIGHT,
    "monitor_sidebar_width_pct": DEFAULT_MONITOR_SIDEBAR_WIDTH_PCT,
    # Logging
    "log_enabled": False,
    "log_level": "INFO",
    # Shell
    "shell_mode": DEFAULT_SHELL_MODE,
    "shell_executable": "",
    # Runtime
    "python_executable": "",
    "conda_env": "",
    "conda_executable": "conda",
    "global_env": {},
    # GPU scheduler
    "gpu_scheduler_enabled": False,
    "gpu_scheduler_task_mode": "single",
    "gpu_scheduler_selection_mode": "auto",
    "gpu_scheduler_gpus_per_task": 1,
    "gpu_scheduler_device_ids": [],
    "gpu_scheduler_memory_used_pct": 40,
    "gpu_scheduler_min_free_memory_gb": 40,
    "gpu_scheduler_compute_used_pct": 30,
    "gpu_scheduler_stable_seconds": 15,
    "gpu_scheduler_max_wait_seconds": 172800,
    "gpu_scheduler_max_tasks_per_gpu": 1,
    "gpu_scheduler_respect_cuda_visible_devices": True,
    "gpu_scheduler_require_same_gpu_model": False,
}


SETTINGS_TEMPLATE = f"""\
# Pyruns Workspace Settings
# Auto-generated on first launch. Edit freely.
# Delete this file to reset all values to defaults.

# Server
ui_port: {SETTINGS_DEFAULTS.get("ui_port")}                    # preferred start port; busy ports auto-increment

# Header
header_refresh_interval: {SETTINGS_DEFAULTS.get("header_refresh_interval")}

# Monitor
monitor_chunk_size: {SETTINGS_DEFAULTS.get("monitor_chunk_size")}            # bytes per chunk
monitor_scrollback: {SETTINGS_DEFAULTS.get("monitor_scrollback")}           # initial tail lines and xterm scrollback rows
monitor_line_height: {SETTINGS_DEFAULTS.get("monitor_line_height")}         # terminal row height multiplier (1.0 = normal)
monitor_sidebar_width_pct: {SETTINGS_DEFAULTS.get("monitor_sidebar_width_pct")}     # monitor sidebar width (% of page)

# Logging
log_enabled: {SETTINGS_DEFAULTS.get("log_enabled")}
log_level: {SETTINGS_DEFAULTS.get("log_level")}                    # DEBUG | INFO | WARNING | ERROR | CRITICAL

# Shell
shell_mode: {SETTINGS_DEFAULTS.get("shell_mode")}                  # follow | custom
shell_executable: {SETTINGS_DEFAULTS.get("shell_executable")}

# Runtime
python_executable: {SETTINGS_DEFAULTS.get("python_executable")}             # absolute Python path; empty = pyruns server Python
conda_env: {SETTINGS_DEFAULTS.get("conda_env")}                     # conda env name; applies to Python and shell tasks
conda_executable: {SETTINGS_DEFAULTS.get("conda_executable")}           # conda executable used by conda_env
global_env: {{}}                       # workspace env overrides; task env overrides this

# GPU scheduler
gpu_scheduler_enabled: {SETTINGS_DEFAULTS.get("gpu_scheduler_enabled")}             # false = run normally; true = wait for eligible local GPUs
gpu_scheduler_task_mode: {SETTINGS_DEFAULTS.get("gpu_scheduler_task_mode")}          # single | multi
gpu_scheduler_selection_mode: {SETTINGS_DEFAULTS.get("gpu_scheduler_selection_mode")} # auto | specified
gpu_scheduler_gpus_per_task: {SETTINGS_DEFAULTS.get("gpu_scheduler_gpus_per_task")}  # used when task_mode is multi
gpu_scheduler_device_ids: []             # auto mode: pool; specified mode: exact GPU IDs
gpu_scheduler_memory_used_pct: {SETTINGS_DEFAULTS.get("gpu_scheduler_memory_used_pct")}      # eligible when memory used is below this percent
gpu_scheduler_min_free_memory_gb: {SETTINGS_DEFAULTS.get("gpu_scheduler_min_free_memory_gb")} # eligible when free memory is at least this many GiB
gpu_scheduler_compute_used_pct: {SETTINGS_DEFAULTS.get("gpu_scheduler_compute_used_pct")}     # eligible when GPU compute is below this percent
gpu_scheduler_stable_seconds: {SETTINGS_DEFAULTS.get("gpu_scheduler_stable_seconds")}         # limits must stay eligible for this long
gpu_scheduler_max_wait_seconds: {SETTINGS_DEFAULTS.get("gpu_scheduler_max_wait_seconds")}     # default 48h
gpu_scheduler_max_tasks_per_gpu: {SETTINGS_DEFAULTS.get("gpu_scheduler_max_tasks_per_gpu")}
gpu_scheduler_respect_cuda_visible_devices: {SETTINGS_DEFAULTS.get("gpu_scheduler_respect_cuda_visible_devices")}
gpu_scheduler_require_same_gpu_model: {SETTINGS_DEFAULTS.get("gpu_scheduler_require_same_gpu_model")}

"""


_cached: Dict[str, Any] = {}
_SETTINGS_FILE_LOCKS: Dict[str, threading.RLock] = {}
_SETTINGS_FILE_LOCKS_GUARD = threading.Lock()
_SETTINGS_LOCK_TIMEOUT_SEC = 5.0
_SETTINGS_LOCK_POLL_SEC = 0.05
_SETTINGS_STALE_LOCK_MIN_AGE_SEC = 30.0
_SETTINGS_LOCK_OWNER_HOST = socket.gethostname().lower()


def _thread_lock_for(path: str) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(path))
    with _SETTINGS_FILE_LOCKS_GUARD:
        lock = _SETTINGS_FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _SETTINGS_FILE_LOCKS[key] = lock
        return lock


def _settings_lock_snapshot(path: str) -> tuple[tuple[int, int, int, int], bytes] | None:
    try:
        with open(path, "rb") as handle:
            info = os.fstat(handle.fileno())
            content = handle.read(4096)
    except OSError:
        return None
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size), content


def _settings_lock_owner_bytes() -> bytes:
    owner = {
        "pid": os.getpid(),
        "process_create_time": get_process_create_time(os.getpid()),
        "host": _SETTINGS_LOCK_OWNER_HOST,
        "token": secrets.token_hex(16),
    }
    return json.dumps(owner, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _settings_lock_owner(content: bytes) -> Dict[str, Any] | None:
    try:
        owner = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(owner, dict):
        return None
    pid = owner.get("pid")
    host = owner.get("host")
    token = owner.get("token")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if not isinstance(host, str) or not host or not isinstance(token, str) or not token:
        return None
    return owner


def _settings_lock_is_stale(
    snapshot: tuple[tuple[int, int, int, int], bytes],
    *,
    min_age_sec: float = _SETTINGS_STALE_LOCK_MIN_AGE_SEC,
) -> bool:
    modified_at = snapshot[0][2] / 1_000_000_000
    age = max(0.0, time.time() - modified_at)
    owner = _settings_lock_owner(snapshot[1])
    if owner is None:
        return age >= max(0.0, min_age_sec)
    if owner["host"].lower() != _SETTINGS_LOCK_OWNER_HOST:
        return False

    pid = owner["pid"]
    if not is_pid_running(pid):
        return True

    expected = owner.get("process_create_time")
    if expected is None:
        return False
    try:
        expected_value = float(expected)
    except (TypeError, ValueError, OverflowError):
        return False
    actual = get_process_create_time(pid)
    if actual is None:
        return False
    return abs(actual - expected_value) > 0.01


def _quarantine_settings_lock(
    lock_path: str,
    expected: tuple[tuple[int, int, int, int], bytes],
) -> bool:
    if _settings_lock_snapshot(lock_path) != expected:
        return False
    quarantine_path = (
        f"{lock_path}.stale-{os.getpid()}-{threading.get_ident()}-{secrets.token_hex(8)}"
    )
    try:
        os.replace(lock_path, quarantine_path)
    except FileNotFoundError:
        return True
    except OSError:
        return False

    if _settings_lock_snapshot(quarantine_path) != expected:
        try:
            if not os.path.exists(lock_path):
                os.replace(quarantine_path, lock_path)
        except OSError:
            pass
        return False
    try:
        os.remove(quarantine_path)
    except FileNotFoundError:
        pass
    except OSError:
        try:
            if not os.path.exists(lock_path):
                os.replace(quarantine_path, lock_path)
        except OSError:
            pass
        return False
    return True


def _remove_stale_settings_lock(lock_path: str) -> bool:
    snapshot = _settings_lock_snapshot(lock_path)
    return bool(
        snapshot is not None
        and _settings_lock_is_stale(snapshot)
        and _quarantine_settings_lock(lock_path, snapshot)
    )


def _release_settings_lock(lock_path: str, owner: bytes) -> None:
    snapshot = _settings_lock_snapshot(lock_path)
    if snapshot is not None and snapshot[1] == owner:
        if _quarantine_settings_lock(lock_path, snapshot):
            return
        if _settings_lock_snapshot(lock_path) == snapshot:
            try:
                os.remove(lock_path)
            except OSError:
                pass


def _open_settings_lock(
    path: str,
    timeout_sec: float = _SETTINGS_LOCK_TIMEOUT_SEC,
) -> tuple[int, str, bytes]:
    lock_path = f"{path}.lock"
    lock_dir = os.path.dirname(path) or "."
    validate_workspace_file(lock_path, lock_dir, label="Settings lock file")
    deadline = time.monotonic() + max(0.0, timeout_sec)
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            validate_workspace_file(lock_path, lock_dir, label="Settings lock file")
            if _remove_stale_settings_lock(lock_path):
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Settings are locked by another process: {lock_path}"
                ) from exc
            time.sleep(_SETTINGS_LOCK_POLL_SEC)
            continue

        owner = _settings_lock_owner_bytes()
        try:
            written = os.write(fd, owner)
            if written != len(owner):
                raise OSError("Could not write the complete settings lock owner")
            os.fsync(fd)
            return fd, lock_path, owner
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            snapshot = _settings_lock_snapshot(lock_path)
            if snapshot is not None:
                _quarantine_settings_lock(lock_path, snapshot)
            raise


def _write_settings_lock(fd: int, text: str) -> None:
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_create_text_file(path: str, text: str) -> bool:
    """Publish a complete new file without replacing an existing target."""

    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    try:
        _write_settings_lock(fd, text)
        try:
            os.link(tmp_path, path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _parse_settings_mapping(text: str, path: str) -> Dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Could not parse settings file '{path}': {exc}") from exc
    if data is None:
        raise ValueError(f"Settings file is empty: {path}")
    if not isinstance(data, dict):
        raise ValueError(f"Settings file root must be a mapping: {path}")
    return data


def setting_numbers_are_finite(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(
            setting_numbers_are_finite(item_key) and setting_numbers_are_finite(item_value)
            for item_key, item_value in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(setting_numbers_are_finite(item) for item in value)
    return True


def _settings_path(root_dir: str = ROOT_DIR) -> str:
    """Resolve settings path.

    If ``root_dir`` is ``.../_pyruns_/<script_name>``, settings live in
    ``.../_pyruns_/_pyruns_settings.yaml``.
    """
    parent = os.path.dirname(os.path.abspath(root_dir))
    if os.path.basename(parent) == DEFAULT_ROOT_NAME:
        return os.path.join(parent, SETTINGS_FILENAME)
    return os.path.join(root_dir, SETTINGS_FILENAME)


def ensure_settings_file(root_dir: str = ROOT_DIR) -> str:
    """Create settings file with defaults if it does not exist."""
    path = _settings_path(root_dir)
    validate_workspace_file(
        path,
        os.path.dirname(path) or ".",
        label="Settings file",
    )
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    validate_workspace_file(
        path,
        os.path.dirname(path) or ".",
        label="Settings file",
    )
    if os.path.lexists(path):
        return path
    _atomic_create_text_file(path, SETTINGS_TEMPLATE)
    validate_workspace_file(
        path,
        os.path.dirname(path) or ".",
        label="Settings file",
    )
    return path


def load_settings(root_dir: str = ROOT_DIR) -> Dict[str, Any]:
    """Load and cache settings from disk with defaults merged in."""
    global _cached
    path = _settings_path(root_dir)
    merged = dict(SETTINGS_DEFAULTS)
    validate_workspace_file(
        path,
        os.path.dirname(path) or ".",
        label="Settings file",
    )

    if os.path.lexists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"Could not read settings file '{path}': {exc}") from exc
        data = _parse_settings_mapping(text, path)
        merged.update(
            (key, value)
            for key, value in data.items()
            if key in SETTINGS_DEFAULTS and setting_numbers_are_finite(value)
        )

    _cached = merged
    return merged


def reload_settings(root_dir: str = ROOT_DIR) -> Dict[str, Any]:
    """Force reload settings from disk."""
    return load_settings(root_dir)


def get(key: str, default: Any = None) -> Any:
    """Get one setting value from cache (lazy-loading if needed)."""
    if not _cached:
        load_settings(ROOT_DIR)

    if not _cached:
        return SETTINGS_DEFAULTS.get(key, default)
    return _cached.get(key, SETTINGS_DEFAULTS.get(key, default))


def save_setting(key: str, value: Any) -> None:
    """Persist a single setting for current ROOT_DIR."""
    save_setting_for_root(ROOT_DIR, key, value)


def _yaml_scalar_to_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, list):
        if not value:
            return "[]"
        return "\n" + yaml.dump(value, default_flow_style=False, allow_unicode=True).rstrip("\n")
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "\n" + yaml.dump(value, default_flow_style=False, allow_unicode=True, sort_keys=False).rstrip("\n")
    if isinstance(value, str):
        rendered = yaml.safe_dump(
            value,
            default_flow_style=True,
            allow_unicode=True,
            width=10_000,
        ).rstrip("\n")
        if rendered.endswith("\n..."):
            rendered = rendered[:-4]
        return rendered
    return str(value)


def _setting_block_pattern(key: str) -> re.Pattern[str]:
    """Match one top-level setting and its indented YAML value."""

    return re.compile(
        rf"^{re.escape(key)}\s*:.*(?:\n[ \t]+(?:-[ \t]+.*|[^:\n]+:.*))*",
        re.MULTILINE,
    )


def save_settings_for_root(root_dir: str, values: Dict[str, Any]) -> None:
    """Persist known settings together with one exclusive, atomic replace."""

    updates = dict(values)
    if not updates:
        return

    path = _settings_path(root_dir)
    validate_workspace_file(
        path,
        os.path.dirname(path) or ".",
        label="Settings file",
    )
    for key, value in updates.items():
        if key not in SETTINGS_DEFAULTS:
            raise KeyError(f"Unknown setting: {key}")
        if not setting_numbers_are_finite(value):
            raise ValueError(f"Setting {key} must contain only finite numbers")

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    validate_workspace_file(
        path,
        os.path.dirname(path) or ".",
        label="Settings file",
    )
    with _thread_lock_for(path):
        lock_fd, lock_path, lock_owner = _open_settings_lock(path)
        tmp_path = ""
        try:
            text = ""
            loaded: Dict[str, Any] = {}
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()

                loaded = yaml.safe_load(text) or {}
                if not isinstance(loaded, dict):
                    raise ValueError(f"Settings file root must be a mapping: {path}")

            has_unknown_keys = any(item_key not in SETTINGS_DEFAULTS for item_key in loaded)
            loaded = {
                item_key: item_value
                for item_key, item_value in loaded.items()
                if item_key in SETTINGS_DEFAULTS
            }

            # Scalars use surgical replacements to preserve template comments.
            # Mapping/list values use one canonical dump, matching the prior
            # single-setting behavior while still committing the whole batch once.
            if has_unknown_keys or any(
                isinstance(value, (dict, list)) for value in updates.values()
            ):
                loaded.update(updates)
                new_text = yaml.safe_dump(
                    loaded,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                )
            else:
                new_text = text
                for key, value in updates.items():
                    val_text = _yaml_scalar_to_text(value)
                    pattern = _setting_block_pattern(key)
                    if pattern.search(new_text):
                        new_text = pattern.sub(lambda _: f"{key}: {val_text}", new_text)
                    else:
                        separator = "" if not new_text or new_text.endswith("\n") else "\n"
                        new_text = f"{new_text}{separator}{key}: {val_text}\n"

            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(path)}.",
                suffix=".tmp",
                dir=os.path.dirname(path) or ".",
                text=True,
            )
            _write_settings_lock(fd, new_text)
            os.replace(tmp_path, path)
            tmp_path = ""
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            try:
                os.close(lock_fd)
            except OSError:
                pass
            _release_settings_lock(lock_path, lock_owner)

        _cached.update(updates)


def save_setting_for_root(root_dir: str, key: str, value: Any) -> None:
    """Persist one known key with an exclusive, atomic replace."""

    save_settings_for_root(root_dir, {key: value})


def unset_setting_for_root(root_dir: str, key: str) -> None:
    """Remove one saved override so the built-in default becomes effective."""

    path = _settings_path(root_dir)
    validate_workspace_file(
        path,
        os.path.dirname(path) or ".",
        label="Settings file",
    )
    if key not in SETTINGS_DEFAULTS:
        raise KeyError(f"Unknown setting: {key}")

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    validate_workspace_file(
        path,
        os.path.dirname(path) or ".",
        label="Settings file",
    )
    with _thread_lock_for(path):
        lock_fd, lock_path, lock_owner = _open_settings_lock(path)
        tmp_path = ""
        try:
            text = ""
            loaded: Dict[str, Any] = {}
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
                loaded = yaml.safe_load(text) or {}
                if not isinstance(loaded, dict):
                    raise ValueError(f"Settings file root must be a mapping: {path}")

            has_unknown_keys = any(item_key not in SETTINGS_DEFAULTS for item_key in loaded)
            known = {
                item_key: item_value
                for item_key, item_value in loaded.items()
                if item_key in SETTINGS_DEFAULTS and item_key != key
            }
            if has_unknown_keys or isinstance(SETTINGS_DEFAULTS[key], (dict, list)):
                new_text = yaml.safe_dump(
                    known,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                )
            else:
                new_text = _setting_block_pattern(key).sub("", text)
                new_text = re.sub(r"\n{3,}", "\n\n", new_text).lstrip("\n")

            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(path)}.",
                suffix=".tmp",
                dir=os.path.dirname(path) or ".",
                text=True,
            )
            _write_settings_lock(fd, new_text)
            os.replace(tmp_path, path)
            tmp_path = ""
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            try:
                os.close(lock_fd)
            except OSError:
                pass
            _release_settings_lock(lock_path, lock_owner)

        _cached[key] = SETTINGS_DEFAULTS[key]
