"""Helpers for task kinds, config file resolution, and task content loading."""

from __future__ import annotations

import os
from typing import Any, Dict, Tuple

from pyruns._config import (
    CONFIG_FILENAME,
    SHELL_CONFIG_FILENAMES,
    TASK_KIND_CONFIG,
    TASK_KIND_SHELL,
    TASK_KIND_TO_CONFIG_FILENAME,
    TASK_KINDS,
    WORKSPACE_KIND_SCRIPT,
    WORKSPACE_KIND_SHELL,
    WORKSPACE_KINDS,
)
from pyruns.utils.config_utils import (
    build_config_preview_and_search_text,
    load_yaml_strict,
    save_yaml,
)

TASK_KIND_ALIASES = {
    "config": TASK_KIND_CONFIG,
    "py": TASK_KIND_CONFIG,
    "python": TASK_KIND_CONFIG,
    TASK_KIND_SHELL: TASK_KIND_SHELL,
}


def normalize_workspace_kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    return kind if kind in WORKSPACE_KINDS else WORKSPACE_KIND_SCRIPT


def normalize_task_kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    return TASK_KIND_ALIASES.get(kind, TASK_KIND_CONFIG)


def is_known_task_kind(value: Any) -> bool:
    kind = str(value or "").strip().lower()
    return not kind or kind in TASK_KIND_ALIASES or kind in TASK_KINDS


def resolve_task_config_file(
    info: Dict[str, Any],
    task_kind: str | None = None,
    task_dir: str | None = None,
) -> str:
    normalized_kind = normalize_task_kind(task_kind or info.get("task_kind", info.get("config_mode")))
    config_file = str(info.get("config_file", "") or "").strip()
    if config_file:
        return config_file
    if normalized_kind == TASK_KIND_SHELL and task_dir:
        for candidate in SHELL_CONFIG_FILENAMES:
            if os.path.exists(os.path.join(task_dir, candidate)):
                return candidate
    return TASK_KIND_TO_CONFIG_FILENAME.get(normalized_kind, CONFIG_FILENAME)


def read_task_payload(task_dir: str, info: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str, str]:
    """Return ``(task_kind, config, config_text, load_error)`` for one task."""

    task_kind = normalize_task_kind(info.get("task_kind", info.get("config_mode")))
    config_file = resolve_task_config_file(info, task_kind, task_dir)
    config_path = os.path.join(task_dir, config_file)

    if not os.path.exists(config_path):
        return task_kind, {}, "", f"{config_file} is missing"

    if task_kind == TASK_KIND_SHELL:
        try:
            with open(config_path, "r", encoding="utf-8") as handle:
                return task_kind, {}, handle.read(), ""
        except Exception as exc:
            return task_kind, {}, "", str(exc)

    try:
        return task_kind, load_yaml_strict(config_path), "", ""
    except Exception as exc:
        return task_kind, {}, "", str(exc)


def write_task_payload(
    task_dir: str,
    *,
    task_kind: str,
    config_file: str,
    config: Dict[str, Any] | None = None,
    config_text: str = "",
) -> None:
    """Persist the task payload using the appropriate on-disk representation."""

    os.makedirs(task_dir, exist_ok=True)
    payload_path = os.path.join(task_dir, config_file)
    if normalize_task_kind(task_kind) == TASK_KIND_SHELL:
        with open(payload_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(str(config_text or ""))
        return
    save_yaml(payload_path, config or {})


def build_task_preview_and_search(
    *,
    task_kind: str,
    config: Dict[str, Any] | None = None,
    config_text: str = "",
    task_name: str = "",
    notes: str = "",
) -> Tuple[str, str]:
    """Return preview/search strings for config or shell tasks."""

    normalized_kind = normalize_task_kind(task_kind)
    if normalized_kind == TASK_KIND_SHELL:
        lines = [
            line.strip()
            for line in str(config_text or "").splitlines()
            if line.strip()
        ]
        preview_source = [line for line in lines if not line.startswith("#")]
        preview = " | ".join(preview_source[:3]) if preview_source else "(empty shell script)"
        if len(preview) > 120:
            preview = preview[:117] + "..."
        search_blob = "\n".join([str(task_name or ""), str(notes or ""), str(config_text or "")]).lower()
        return preview, search_blob

    return build_config_preview_and_search_text(
        config or {},
        task_name=task_name,
        notes=notes,
    )
