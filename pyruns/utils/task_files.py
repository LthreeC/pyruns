"""Helpers for task kinds, config file resolution, and task content loading."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Dict, List, Tuple

from omegaconf import DictConfig, OmegaConf

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
    flatten_dict,
    load_config_text,
    save_yaml,
)
from pyruns.utils.info_io import (
    validate_task_directory,
    validate_workspace_file,
)
from pyruns.utils.sort_utils import normalize_task_search_text, task_search_needles

MAX_TASK_PAYLOAD_BYTES = 4 * 1024 * 1024

TASK_KIND_ALIASES = {
    "config": TASK_KIND_CONFIG,
    "py": TASK_KIND_CONFIG,
    "python": TASK_KIND_CONFIG,
    TASK_KIND_SHELL: TASK_KIND_SHELL,
}


def _empty_config() -> DictConfig:
    config = OmegaConf.create({})
    if not isinstance(config, DictConfig):
        raise RuntimeError("OmegaConf did not create a mapping config")
    return config


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


def read_task_payload(task_dir: str, info: Dict[str, Any]) -> Tuple[str, DictConfig, str, str]:
    """Return ``(task_kind, config, config_text, load_error)`` for one task."""

    task_kind = normalize_task_kind(info.get("task_kind", info.get("config_mode")))
    config_file = resolve_task_config_file(info, task_kind, task_dir)
    try:
        config_path = resolve_task_payload_path(task_dir, config_file)
    except ValueError as exc:
        return task_kind, _empty_config(), "", str(exc)

    if not os.path.exists(config_path):
        return task_kind, _empty_config(), "", f"{config_file} is missing"

    if task_kind == TASK_KIND_SHELL:
        try:
            return task_kind, _empty_config(), _read_text_limited(config_path), ""
        except Exception as exc:
            return task_kind, _empty_config(), "", str(exc)

    try:
        parsed = load_config_text(_read_text_limited(config_path))
        if not isinstance(parsed, DictConfig):
            raise ValueError(f"YAML root must be a mapping: {config_path}")
        return task_kind, parsed, "", ""
    except Exception as exc:
        return task_kind, _empty_config(), "", str(exc)


def write_task_payload(
    task_dir: str,
    *,
    task_kind: str,
    config_file: str,
    config: Dict[str, Any] | DictConfig | None = None,
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
    config: Dict[str, Any] | DictConfig | None = None,
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


_TASK_SEARCH_MATCH_LIMIT = 8
_TASK_SEARCH_SNIPPET_CHARS = 180


def _normalized_search_with_positions(text: str) -> Tuple[str, List[int]]:
    """Normalize text while retaining a map back to source character offsets."""

    normalized: List[str] = []
    positions: List[int] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == ":":
            while normalized and normalized[-1].isspace() and normalized[-1] not in "\r\n":
                normalized.pop()
                positions.pop()
            normalized.append(char)
            positions.append(index)
            index += 1
            while index < len(text) and text[index].isspace() and text[index] not in "\r\n":
                index += 1
            continue

        for lowered in char.lower():
            normalized.append(lowered)
            positions.append(index)
        index += 1
    return "".join(normalized), positions


def _build_task_search_snippet(
    display: str,
    positions: List[int] | None,
    match_index: int,
    match_length: int,
    max_chars: int,
) -> Tuple[str, int, int]:
    if positions is None:
        source_start = match_index
        source_end = match_index + match_length
    else:
        source_start = positions[match_index]
        source_end = positions[min(len(positions) - 1, match_index + match_length - 1)] + 1
    if len(display) <= max_chars:
        return display, source_start, source_end

    body_limit = max(1, max_chars - 6)
    context_before = max(0, min(source_start, body_limit // 3))
    body_start = max(0, source_start - context_before)
    body_end = min(len(display), body_start + body_limit)
    if source_end > body_end:
        body_end = min(len(display), source_end)
        body_start = max(0, body_end - body_limit)

    prefix = "..." if body_start > 0 else ""
    suffix = "..." if body_end < len(display) else ""
    snippet = f"{prefix}{display[body_start:body_end]}{suffix}"
    match_start = len(prefix) + max(0, source_start - body_start)
    match_end = len(prefix) + max(0, min(body_end, source_end) - body_start)
    return snippet, min(match_start, len(snippet)), min(match_end, len(snippet))


def _task_search_source_matches(
    text: str,
    needles: List[str],
    max_chars: int,
    limit: int,
) -> Tuple[int, List[Tuple[int, str, int, int]]]:
    display = str(text or "").replace("\t", "    ").strip()
    if not display:
        return 0, []

    normalized = normalize_task_search_text(display)
    positions: List[int] | None = None
    if not (display.isascii() and len(normalized) == len(display)):
        normalized, positions = _normalized_search_with_positions(display)
    if not normalized:
        return 0, []

    occurrences: List[Tuple[int, int, int]] = []
    match_count = 0
    for needle_index, needle in enumerate(needles):
        match_count += normalized.count(needle)
        if limit <= 0:
            continue
        search_start = 0
        needle_contexts = 0
        while search_start <= len(normalized) and needle_contexts < limit:
            match_index = normalized.find(needle, search_start)
            if match_index < 0:
                break
            occurrences.append((match_index, needle_index, len(needle)))
            needle_contexts += 1
            search_start = match_index + max(1, len(needle))

    contexts: List[Tuple[int, str, int, int]] = []
    for match_index, needle_index, match_length in sorted(occurrences)[:limit]:
        snippet, match_start, match_end = _build_task_search_snippet(
            display,
            positions,
            match_index,
            match_length,
            max_chars,
        )
        contexts.append((needle_index, snippet, match_start, match_end))
    return match_count, contexts


def _task_search_sources(task: Mapping[str, Any]):
    name = str(task.get("name", "") or "")
    if name:
        yield "name", "", name

    notes = str(task.get("notes", "") or "")
    note_lines = notes.splitlines()
    for line_number, line in enumerate(note_lines, start=1):
        if line.strip():
            location = f"Line {line_number}" if len(note_lines) > 1 else ""
            yield "notes", location, line

    if normalize_task_kind(task.get("task_kind")) == TASK_KIND_SHELL:
        for line_number, line in enumerate(str(task.get("config_text", "") or "").splitlines(), start=1):
            if line.strip():
                yield "script", f"Line {line_number}", line
        return

    config = task.get("config", {}) or {}
    if not isinstance(config, (Mapping, DictConfig)):
        return
    for key, value in flatten_dict(config).items():
        key_text = str(key)
        if key_text.startswith("_meta"):
            continue
        detail_lines = f"{key_text}: {value}".splitlines()
        for line in detail_lines:
            if line.strip():
                yield "config", key_text, line


def build_task_search_matches(
    task: Mapping[str, Any],
    query: str,
    *,
    limit: int = _TASK_SEARCH_MATCH_LIMIT,
    max_snippet_chars: int = _TASK_SEARCH_SNIPPET_CHARS,
) -> List[Dict[str, Any]]:
    """Build bounded, display-ready match context for one filtered task."""

    return build_task_search_result(
        task,
        query,
        limit=limit,
        max_snippet_chars=max_snippet_chars,
    )["matches"]


def build_task_search_result(
    task: Mapping[str, Any],
    query: str,
    *,
    limit: int = _TASK_SEARCH_MATCH_LIMIT,
    max_snippet_chars: int = _TASK_SEARCH_SNIPPET_CHARS,
) -> Dict[str, Any]:
    """Return bounded contexts and the exact in-memory match count for one task."""

    needles = task_search_needles(query)
    if not needles:
        return {"matches": [], "match_count": 0}

    safe_limit = max(0, int(limit))
    snippet_chars = max(32, int(max_snippet_chars))
    matches: List[Dict[str, Any]] = []
    match_count = 0
    for field, location, source in _task_search_sources(task):
        source_count, contexts = _task_search_source_matches(
            source,
            needles,
            snippet_chars,
            safe_limit - len(matches),
        )
        match_count += source_count
        for _needle_index, snippet, match_start, match_end in contexts:
            matches.append(
                {
                    "field": field,
                    "location": location,
                    "snippet": snippet,
                    "match_start": match_start,
                    "match_end": match_end,
                }
            )
    return {"matches": matches, "match_count": match_count}
