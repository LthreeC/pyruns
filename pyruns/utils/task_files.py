"""Helpers for task kinds, config file resolution, and task content loading."""

from __future__ import annotations

import os
from typing import Any, Dict, Tuple

import yaml

from pyruns._config import (
    CONFIG_FILENAME,
    SHELL_CONFIG_FILENAMES,
    TASK_KIND_CONFIG,
    TASK_KIND_SHELL,
    TASK_KIND_TO_CONFIG_FILENAME,
    TASK_KINDS,
    WORKSPACE_KIND_SCRIPT,
    WORKSPACE_KINDS,
)
from pyruns.utils.config_utils import (
    build_config_preview_and_search_text,
    save_yaml,
)
from pyruns.utils.info_io import (
    validate_task_directory,
    validate_workspace_file,
)

MAX_TASK_PAYLOAD_BYTES = 4 * 1024 * 1024

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


def resolve_task_payload_path(task_dir: str, config_file: str) -> str:
    validate_task_directory(task_dir)
    base = os.path.abspath(task_dir)
    lexical_parent = os.path.abspath(os.path.dirname(base))
    try:
        if os.path.normcase(os.path.commonpath([base, lexical_parent])) != os.path.normcase(lexical_parent):
            raise ValueError("Task directory resolves outside the tasks directory")
    except (OSError, ValueError) as exc:
        raise ValueError("Task directory resolves outside the tasks directory") from exc

    candidate = os.path.abspath(os.path.join(task_dir, config_file))
    try:
        contained = os.path.normcase(os.path.commonpath([candidate, base])) == os.path.normcase(base)
    except (OSError, ValueError):
        contained = False
    if not contained or candidate == base:
        raise ValueError(f"Config file resolves outside the task directory: {config_file}")
    validate_workspace_file(candidate, base, label="Task payload")
    return candidate


def _read_text_limited(path: str, *, max_bytes: int = MAX_TASK_PAYLOAD_BYTES) -> str:
    with open(path, "rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"Task payload is too large (max {max_bytes} bytes): {path}")
    return raw.decode("utf-8")


def read_task_payload(task_dir: str, info: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str, str]:
    """Return ``(task_kind, config, config_text, load_error)`` for one task."""

    task_kind = normalize_task_kind(info.get("task_kind", info.get("config_mode")))
    config_file = resolve_task_config_file(info, task_kind, task_dir)
    try:
        config_path = resolve_task_payload_path(task_dir, config_file)
    except ValueError as exc:
        return task_kind, {}, "", str(exc)

    if not os.path.exists(config_path):
        return task_kind, {}, "", f"{config_file} is missing"

    if task_kind == TASK_KIND_SHELL:
        try:
            return task_kind, {}, _read_text_limited(config_path), ""
        except Exception as exc:
            return task_kind, {}, "", str(exc)

    try:
        parsed = yaml.safe_load(_read_text_limited(config_path))
        if parsed is None:
            parsed = {}
        if not isinstance(parsed, dict):
            raise ValueError(f"YAML root must be a mapping: {config_path}")
        return task_kind, parsed, "", ""
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

    validate_task_directory(task_dir)
    os.makedirs(task_dir, exist_ok=True)
    validate_task_directory(task_dir)
    payload_path = resolve_task_payload_path(task_dir, config_file)
    if normalize_task_kind(task_kind) == TASK_KIND_SHELL:
        encoded = str(config_text or "").encode("utf-8")
        if len(encoded) > MAX_TASK_PAYLOAD_BYTES:
            raise ValueError(
                f"Task payload is too large (max {MAX_TASK_PAYLOAD_BYTES} bytes): {payload_path}"
            )
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
