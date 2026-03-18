"""Shared launcher discovery and workspace bootstrap helpers."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from pyruns import ensure_config_default
from pyruns._config import CONFIG_DEFAULT_FILENAME, DEFAULT_ROOT_NAME, ENV_KEY_ROOT
from pyruns.utils.info_io import load_script_info
from pyruns.utils.parse_utils import (
    detect_config_source_fast,
    extract_argparse_params,
    generate_config_file,
    resolve_config_path,
)
from pyruns.utils.settings import ensure_settings_file


def normalize_path(path: str) -> str:
    """Return a normalized absolute path with forward slashes."""

    return os.path.abspath(path).replace("\\", "/")


def workspace_root_for_script(script_path: str) -> str:
    """Return the canonical workspace path for a script."""

    normalized = normalize_path(script_path)
    script_dir = os.path.dirname(normalized)
    script_base = os.path.splitext(os.path.basename(normalized))[0]
    return normalize_path(os.path.join(script_dir, DEFAULT_ROOT_NAME, script_base))


def resolve_workspace_for_script(script_path: str) -> str | None:
    """Return the existing workspace for *script_path* if present."""

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
        script_info_path = os.path.join(workspace, "script_info.json")
        if not os.path.isfile(script_info_path):
            continue
        try:
            with open(script_info_path, "r", encoding="utf-8") as handle:
                info = json.load(handle)
        except Exception:
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
            script_info_path = workspace / "script_info.json"
            if not script_info_path.is_file():
                continue
            try:
                info = json.loads(script_info_path.read_text(encoding="utf-8"))
            except Exception:
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


def list_config_candidates(script_path: str) -> list[dict[str, Any]]:
    """Return YAML config candidates for a script."""

    normalized = normalize_path(script_path)
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

    for pattern in ("*.yaml", "*.yml"):
        for config_file in sorted(script_dir.glob(pattern)):
            path = normalize_path(str(config_file))
            if path in items:
                continue
            items[path] = {
                "path": path,
                "label": config_file.name,
                "kind": "script_dir",
            }

    return sorted(
        items.values(),
        key=lambda item: (item["kind"] != "workspace_default", str(item["label"]).lower()),
    )


def list_workspace_candidates(script_path: str, config_path: str | None = None) -> list[dict[str, Any]]:
    """Return the inferred workspace destination for a script/config pair."""

    normalized = normalize_path(script_path)
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


def bootstrap_workspace(script_path: str, custom_yaml: str | None = None) -> str:
    """Prepare a script workspace and optionally import a selected YAML config."""

    filepath = normalize_path(script_path)
    file_dir = os.path.dirname(filepath)
    script_base = os.path.splitext(os.path.basename(filepath))[0]
    pyruns_dir = normalize_path(os.path.join(file_dir, DEFAULT_ROOT_NAME))
    script_dir = normalize_path(os.path.join(pyruns_dir, script_base))

    os.makedirs(script_dir, exist_ok=True)
    ensure_settings_file(pyruns_dir)

    script_info_path = os.path.join(script_dir, "script_info.json")
    if not os.path.exists(script_info_path):
        script_info = {
            "script_name": script_base,
            "script_path": filepath,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(script_info_path, "w", encoding="utf-8") as handle:
            json.dump(script_info, handle, indent=4)

    config_default_path = os.path.join(script_dir, CONFIG_DEFAULT_FILENAME)
    mode, _ = detect_config_source_fast(filepath)

    if custom_yaml:
        yaml_path = resolve_config_path(custom_yaml, file_dir)
        if not yaml_path or not os.path.exists(yaml_path):
            raise FileNotFoundError(f"Custom config '{custom_yaml}' not found.")
        shutil.copy2(yaml_path, config_default_path)
    elif mode == "argparse":
        params = extract_argparse_params(filepath)
        generate_config_file(script_dir, filepath, params)
    elif mode == "pyruns_load" and not os.path.exists(config_default_path):
        # Allow the UI launcher to open the workspace even before a default config exists.
        # Generator can import or create the working YAML later.
        pass

    if mode == "argparse":
        ensure_config_default(script_dir)

    os.environ[ENV_KEY_ROOT] = script_dir
    return script_dir


def read_workspace_summary(workspace_path: str) -> dict[str, Any]:
    """Load script metadata for a workspace path."""

    normalized = normalize_path(workspace_path)
    script_info = load_script_info(normalized)
    return {
        "workspace_path": normalized,
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


def bootstrap_from_cli(script_path: str, custom_yaml: str | None = None) -> str:
    """CLI wrapper for workspace bootstrap that exits on user-facing errors."""

    try:
        return bootstrap_workspace(script_path, custom_yaml)
    except FileNotFoundError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

