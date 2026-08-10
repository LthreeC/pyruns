"""Shared launcher discovery and workspace bootstrap helpers."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from pyruns import ensure_config_default
from pyruns._config import (
    ACTIVE_WORKSPACE_FILENAME,
    CONFIG_DEFAULT_FILENAME,
    DEFAULT_ROOT_NAME,
    ENV_KEY_ROOT,
    SCRIPT_INFO_FILENAME,
    SHELL_WORKSPACE_NAME,
    TASKS_DIR,
    WORKSPACE_KIND_SCRIPT,
    WORKSPACE_KIND_SHELL,
)
from pyruns.utils.info_io import (
    load_script_info,
    save_script_info,
    validate_tasks_root,
    validate_workspace_directory,
    validate_workspace_file,
)
from pyruns.utils.config_utils import load_yaml_strict
from pyruns.utils.parse_utils import (
    detect_config_source_fast,
    extract_argparse_params,
    generate_config_file,
    resolve_config_path,
)
from pyruns.utils.settings import ensure_settings_file


def normalize_path(path: str) -> str:
    """Return a normalized absolute path with forward slashes."""

    expanded = os.path.expandvars(os.path.expanduser(str(path)))
    return os.path.abspath(expanded).replace("\\", "/")


def validate_python_script_path(script_path: str) -> str:
    """Return a normalized Python script path or raise a user-facing error."""

    filepath = normalize_path(script_path)
    if not os.path.isfile(filepath) or Path(filepath).suffix.lower() != ".py":
        raise FileNotFoundError(f"Python script '{script_path}' not found or is not a .py file.")
    return filepath


def native_picker_available() -> bool:
    """Return whether this process can safely open a native file picker."""

    if os.name == "nt" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def workspace_root_parent_for_script(script_path: str) -> str:
    """Return the project-level ``_pyruns_`` directory for a script."""

    normalized = normalize_path(script_path)
    script_dir = os.path.dirname(normalized)
    return normalize_path(os.path.join(script_dir, DEFAULT_ROOT_NAME))


def workspace_name_for_script_base(script_base: str) -> str:
    """Return a filesystem-safe workspace directory name for a Python script."""

    if str(script_base).casefold() == WORKSPACE_KIND_SHELL:
        raise ValueError(
            "Python script name 'shell.py' is reserved for the shell workspace selector"
        )
    return f"py{SHELL_WORKSPACE_NAME}" if script_base == SHELL_WORKSPACE_NAME else script_base


def workspace_root_for_script(script_path: str) -> str:
    """Return the canonical script workspace path for a script."""

    normalized = normalize_path(script_path)
    script_base = os.path.splitext(os.path.basename(normalized))[0]
    workspace_name = workspace_name_for_script_base(script_base)
    return normalize_path(os.path.join(workspace_root_parent_for_script(normalized), workspace_name))


def shell_workspace_root_for_run_root(run_root: str) -> str:
    """Return the canonical shell workspace path for a project workspace root."""

    normalized = normalize_path(run_root)
    if os.path.basename(normalized) == SHELL_WORKSPACE_NAME:
        return normalized
    if os.path.basename(normalized) == DEFAULT_ROOT_NAME:
        return normalize_path(os.path.join(normalized, SHELL_WORKSPACE_NAME))
    return normalize_path(os.path.join(os.path.dirname(normalized), SHELL_WORKSPACE_NAME))


def shell_project_root_for_workspace(shell_root: str) -> str:
    """Return the user project directory represented by a shell workspace."""

    normalized = normalize_path(shell_root)
    parent_root = os.path.dirname(normalized)
    if os.path.basename(normalized) == SHELL_WORKSPACE_NAME and os.path.basename(parent_root) == DEFAULT_ROOT_NAME:
        return normalize_path(os.path.dirname(parent_root))
    return normalize_path(os.path.dirname(normalized))


def _read_script_info(path: Path) -> dict[str, Any]:
    return load_script_info(str(path.parent))


def _is_shell_workspace_info(info: dict[str, Any], workspace_name: str) -> bool:
    return (
        str(info.get("workspace_kind", "") or "") == WORKSPACE_KIND_SHELL
        or workspace_name == SHELL_WORKSPACE_NAME
    )


def resolve_workspace_for_script(script_path: str) -> str | None:
    """Return the existing script workspace for *script_path* if present."""

    normalized = normalize_path(script_path)
    candidate = workspace_root_for_script(normalized)
    if os.path.isdir(candidate):
        return candidate

    pyruns_dir = os.path.dirname(candidate)
    if not os.path.isdir(pyruns_dir):
        return None

    target_base = os.path.splitext(os.path.basename(normalized))[0]
    for entry in sorted(os.listdir(pyruns_dir)):
        workspace = os.path.join(pyruns_dir, entry)
        script_info_path = os.path.join(workspace, SCRIPT_INFO_FILENAME)
        if not os.path.isfile(script_info_path):
            continue
        info = load_script_info(workspace)
        if not info:
            continue
        if _is_shell_workspace_info(info, entry):
            continue
        if info.get("script_name") == target_base or normalize_path(str(info.get("script_path", ""))) == normalized:
            return normalize_path(workspace)
    return None


def list_script_candidates(base_dir: str | None = None) -> list[dict[str, Any]]:
    """Discover launchable scripts from the current project directory."""

    root = Path(base_dir or os.getcwd()).resolve()
    items: dict[str, dict[str, Any]] = {}
    workspace_root = root / DEFAULT_ROOT_NAME

    if workspace_root.is_dir():
        for workspace in sorted(path for path in workspace_root.iterdir() if path.is_dir()):
            script_info_path = workspace / SCRIPT_INFO_FILENAME
            if not script_info_path.is_file():
                continue
            info = _read_script_info(script_info_path)
            if not info or _is_shell_workspace_info(info, workspace.name):
                continue
            script_path = normalize_path(str(info.get("script_path", "") or ""))
            script_name = str(info.get("script_name", "") or workspace.name)
            workspace_path = normalize_path(str(workspace))
            items[script_path or workspace_path] = {
                "script_path": script_path,
                "script_name": script_name,
                "label": script_name,
                "workspace_path": workspace_path,
                "source": "workspace",
            }

    for script_file in sorted(root.glob("*.py")):
        script_path = normalize_path(str(script_file))
        existing = items.get(script_path)
        if existing is not None:
            existing["source"] = "workspace+file"
            continue
        items[script_path] = {
            "script_path": script_path,
            "script_name": script_file.stem,
            "label": script_file.name,
            "workspace_path": resolve_workspace_for_script(script_path) or workspace_root_for_script(script_path),
            "source": "file",
        }

    ordered = sorted(
        items.values(),
        key=lambda item: (
            item.get("source") != "workspace+file",
            item.get("source") != "workspace",
            str(item.get("label", "")).lower(),
        ),
    )
    return ordered


def choose_script_file(initial_dir: str | None = None) -> str | None:
    """Open a native file picker and return one Python script path."""

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            initialdir=initial_dir or os.getcwd(),
            title="Select Python script",
            filetypes=[("Python files", "*.py"), ("All files", "*.*")],
        )
        root.destroy()
        return normalize_path(path) if path else None
    except Exception:
        return None


def choose_config_file(initial_dir: str | None = None) -> str | None:
    """Open a native file picker and return one YAML config path."""

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            initialdir=initial_dir or os.getcwd(),
            title="Select YAML config",
            filetypes=[
                ("YAML files", "*.yaml *.yml"),
                ("All files", "*.*"),
            ],
        )
        root.destroy()
        return normalize_path(path) if path else None
    except Exception:
        return None


def choose_shell_file(initial_dir: str | None = None) -> str | None:
    """Open a native file picker and return one shell script path."""

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            initialdir=initial_dir or os.getcwd(),
            title="Select shell script",
            filetypes=[
                ("Shell scripts", "*.sh *.bash *.zsh *.fish *.ps1 *.bat *.cmd"),
                ("All files", "*.*"),
            ],
        )
        root.destroy()
        return normalize_path(path) if path else None
    except Exception:
        return None


def choose_directory(initial_dir: str | None = None) -> str | None:
    """Open a native directory picker and return one folder path."""

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(
            initialdir=initial_dir or os.getcwd(),
            title="Select shell workspace directory",
            mustexist=False,
        )
        root.destroy()
        return normalize_path(path) if path else None
    except Exception:
        return None


def list_config_candidates(script_path: str) -> list[dict[str, Any]]:
    """Return YAML config candidates for a script."""

    normalized = validate_python_script_path(script_path)
    script_dir = Path(normalized).parent
    workspace_path = resolve_workspace_for_script(normalized) or workspace_root_for_script(normalized)
    workspace_default = Path(workspace_path) / CONFIG_DEFAULT_FILENAME
    items: dict[str, dict[str, Any]] = {}

    if workspace_default.is_file():
        path = normalize_path(str(workspace_default))
        items[path] = {
            "path": path,
            "label": "Workspace default",
            "kind": "workspace_default",
        }

    search_roots: list[tuple[Path, str]] = [(script_dir, "script_dir")]
    for folder_name in ("configs", "config", "conf"):
        candidate_dir = script_dir / folder_name
        if candidate_dir.is_dir():
            search_roots.append((candidate_dir, "script_config_dir"))

    for search_root, kind in search_roots:
        for pattern in ("*.yaml", "*.yml"):
            for config_file in sorted(search_root.glob(pattern)):
                path = normalize_path(str(config_file))
                if path in items:
                    continue
                if search_root == script_dir:
                    label = config_file.name
                else:
                    label = config_file.relative_to(script_dir).as_posix()
                items[path] = {
                    "path": path,
                    "label": label,
                    "kind": kind,
                }

    return sorted(
        items.values(),
        key=lambda item: (item["kind"] != "workspace_default", str(item["label"]).lower()),
    )


def get_config_selection_metadata(script_path: str) -> dict[str, Any]:
    """Return launcher metadata about whether a script needs an initial YAML."""

    normalized = validate_python_script_path(script_path)
    workspace_path = resolve_workspace_for_script(normalized) or workspace_root_for_script(normalized)
    workspace_default = Path(workspace_path) / CONFIG_DEFAULT_FILENAME
    mode, _ = detect_config_source_fast(normalized)
    return {
        "config_source": mode,
        "requires_config_template": mode == "pyruns_load" and not workspace_default.is_file(),
    }


def list_workspace_candidates(script_path: str, config_path: str | None = None) -> list[dict[str, Any]]:
    """Return the inferred script workspace destination for a script/config pair."""

    normalized = validate_python_script_path(script_path)
    workspace_path = resolve_workspace_for_script(normalized) or workspace_root_for_script(normalized)
    config_name = Path(config_path).name if config_path else ""
    return [
        {
            "workspace_path": workspace_path,
            "script_path": normalized,
            "script_name": Path(normalized).stem,
            "config_path": normalize_path(config_path) if config_path else "",
            "config_name": config_name,
            "exists": os.path.isdir(workspace_path),
        }
    ]


def _write_script_info(workspace_path: str, payload: dict[str, Any]) -> None:
    save_script_info(workspace_path, payload)
    _fsync_parent_directory(os.path.join(workspace_path, SCRIPT_INFO_FILENAME))


def _fsync_parent_directory(path: str) -> None:
    """Persist a replaced directory entry where the platform supports it."""

    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
    try:
        fd = os.open(os.path.dirname(os.path.abspath(path)), flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_text(path: str, value: str) -> None:
    """Atomically replace one small text file without exposing truncated content."""

    parent = os.path.dirname(os.path.abspath(path))
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temp_path, path)
                break
            except PermissionError:
                if attempt >= 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
        _fsync_parent_directory(path)
    finally:
        if os.path.lexists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _atomic_copy_file(source: str, destination: str) -> None:
    """Copy one file without ever opening the destination through a link."""

    parent = os.path.dirname(os.path.abspath(destination))
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(destination)}.",
        suffix=".tmp",
        dir=parent,
    )
    os.close(fd)
    try:
        shutil.copyfile(source, temp_path)
        with open(temp_path, "r+b") as handle:
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temp_path, destination)
                break
            except PermissionError:
                if attempt >= 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
        _fsync_parent_directory(destination)
    finally:
        if os.path.lexists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def mark_workspace_active(workspace_path: str) -> str:
    """Record the workspace most recently selected in its project root."""

    normalized = normalize_path(workspace_path)
    workspace_parent = os.path.dirname(normalized)
    validate_workspace_directory(normalized)
    validate_workspace_directory(workspace_parent)
    marker_path = os.path.join(workspace_parent, ACTIVE_WORKSPACE_FILENAME)
    validate_workspace_file(
        marker_path,
        workspace_parent,
        label=ACTIVE_WORKSPACE_FILENAME,
    )
    _atomic_write_text(marker_path, os.path.basename(normalized))
    validate_workspace_file(
        marker_path,
        workspace_parent,
        label=ACTIVE_WORKSPACE_FILENAME,
    )
    return normalized


def bootstrap_workspace(
    script_path: str,
    custom_yaml: str | None = None,
    *,
    preserve_default: bool = False,
) -> str:
    """Prepare a script workspace and optionally import a selected YAML config."""

    filepath = validate_python_script_path(script_path)
    file_dir = os.path.dirname(filepath)
    script_base = os.path.splitext(os.path.basename(filepath))[0]
    pyruns_dir = workspace_root_parent_for_script(filepath)
    script_dir = workspace_root_for_script(filepath)

    validate_workspace_directory(script_dir)
    config_default_path = normalize_path(os.path.join(script_dir, CONFIG_DEFAULT_FILENAME))
    validate_workspace_file(
        config_default_path,
        script_dir,
        label=CONFIG_DEFAULT_FILENAME,
    )
    mode, _ = detect_config_source_fast(filepath)
    resolved_custom_yaml = ""

    if custom_yaml:
        yaml_path = resolve_config_path(custom_yaml, file_dir)
        if not yaml_path or not os.path.exists(yaml_path):
            raise FileNotFoundError(f"Custom config '{custom_yaml}' not found.")
        resolved_custom_yaml = normalize_path(yaml_path)
        load_yaml_strict(resolved_custom_yaml)
        if resolved_custom_yaml == config_default_path:
            resolved_custom_yaml = ""

    keep_existing_default = preserve_default and os.path.exists(config_default_path)
    argparse_params = None
    if mode == "argparse" and not resolved_custom_yaml and not keep_existing_default:
        argparse_params = extract_argparse_params(filepath)
    elif (
        mode == "pyruns_load"
        and not resolved_custom_yaml
        and not os.path.exists(config_default_path)
    ):
        raise FileNotFoundError(
            "This script uses pyruns.load() and needs a YAML template on first launch. "
            "Choose a YAML config in the Launcher, or run "
            "`pyr init <script.py> --config <config.yaml>` once. "
            f"Later `pyr ui <script.py>` will reuse `{CONFIG_DEFAULT_FILENAME}` automatically."
        )

    existing = load_script_info(script_dir)
    script_info = {
        "workspace_kind": WORKSPACE_KIND_SCRIPT,
        "script_name": script_base,
        "script_path": filepath,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if existing.get("last_used_template"):
        script_info["last_used_template"] = existing["last_used_template"]

    os.makedirs(script_dir, exist_ok=True)
    validate_workspace_directory(script_dir)
    tasks_dir = os.path.join(script_dir, TASKS_DIR)
    validate_tasks_root(tasks_dir)
    os.makedirs(tasks_dir, exist_ok=True)
    validate_tasks_root(tasks_dir)
    ensure_settings_file(pyruns_dir)

    if resolved_custom_yaml and not keep_existing_default:
        _atomic_copy_file(resolved_custom_yaml, config_default_path)
        script_info["config_default_source"] = resolved_custom_yaml
        script_info["config_default_source_name"] = os.path.basename(resolved_custom_yaml)
    elif mode == "argparse" and not keep_existing_default:
        generate_config_file(script_dir, filepath, argparse_params or {})
    elif existing.get("config_default_source"):
        script_info["config_default_source"] = existing["config_default_source"]
        script_info["config_default_source_name"] = existing.get(
            "config_default_source_name",
            os.path.basename(str(existing["config_default_source"])),
        )

    if mode == "argparse":
        ensure_config_default(script_dir)

    _write_script_info(script_dir, script_info)
    os.environ[ENV_KEY_ROOT] = script_dir
    return mark_workspace_active(script_dir)


def bootstrap_shell_workspace(run_root: str) -> str:
    """Prepare the project-level shell workspace derived from the current run root."""

    shell_root = shell_workspace_root_for_run_root(run_root)
    parent_root = os.path.dirname(shell_root)
    project_root = shell_project_root_for_workspace(shell_root)

    validate_workspace_directory(shell_root)
    os.makedirs(shell_root, exist_ok=True)
    validate_workspace_directory(shell_root)
    tasks_dir = os.path.join(shell_root, TASKS_DIR)
    validate_tasks_root(tasks_dir)
    os.makedirs(tasks_dir, exist_ok=True)
    validate_tasks_root(tasks_dir)
    ensure_settings_file(parent_root)

    existing = load_script_info(shell_root)
    payload = {
        "workspace_kind": WORKSPACE_KIND_SHELL,
        "script_name": SHELL_WORKSPACE_NAME,
        "script_path": "",
        "project_root": project_root,
        "last_used_template": "",
        "created_at": existing.get("created_at", time.strftime("%Y-%m-%d %H:%M:%S")),
    }
    _write_script_info(shell_root, payload)

    os.environ[ENV_KEY_ROOT] = shell_root
    return mark_workspace_active(shell_root)


def read_workspace_summary(workspace_path: str) -> dict[str, Any]:
    """Load workspace metadata for a workspace path."""

    normalized = normalize_path(workspace_path)
    script_info = load_script_info(normalized)
    return {
        "workspace_path": normalized,
        "workspace_kind": str(script_info.get("workspace_kind", "") or ""),
        "script_name": str(script_info.get("script_name", "") or ""),
        "script_path": str(script_info.get("script_path", "") or ""),
    }


def launcher_query(script_path: str | None = None, config_path: str | None = None) -> str:
    """Build a lightweight launcher query string for browser startup."""

    params: list[tuple[str, str]] = [("launcher", "1")]
    if script_path:
        params.append(("script", normalize_path(script_path)))
    if config_path:
        params.append(("config", normalize_path(config_path)))
    return f"/?{urlencode(params)}"


def bootstrap_from_cli(
    script_path: str,
    custom_yaml: str | None = None,
    *,
    preserve_default: bool = False,
) -> str:
    """CLI wrapper for workspace bootstrap that exits on user-facing errors."""

    try:
        if preserve_default:
            return bootstrap_workspace(script_path, custom_yaml, preserve_default=True)
        return bootstrap_workspace(script_path, custom_yaml)
    except FileNotFoundError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
