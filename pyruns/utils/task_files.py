"""Helpers for task kinds, config file resolution, and task content loading."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, BinaryIO, Dict, List, NamedTuple, Tuple, TypeVar, TypedDict

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
    iter_config_fields,
    load_config_text,
    load_config_view_text,
    save_yaml,
)
from pyruns.utils.info_io import (
    validate_task_directory,
    validate_workspace_file,
)
from pyruns.utils.file_io import read_bounded_bytes
from pyruns.utils.sort_utils import filter_tasks
from pyruns.utils.search_query import SearchQuery

MAX_TASK_PAYLOAD_BYTES = 4 * 1024 * 1024

TASK_KIND_ALIASES = {
    "config": TASK_KIND_CONFIG,
    "py": TASK_KIND_CONFIG,
    "python": TASK_KIND_CONFIG,
    TASK_KIND_SHELL: TASK_KIND_SHELL,
}

_Task = TypeVar("_Task", bound=Mapping[str, object])


class TaskPayloadSnapshot(NamedTuple):
    task_kind: str
    config: DictConfig | Dict[str, Any]
    config_text: str
    load_error: str
    signature: tuple[int, ...] | None


class TaskSearchMatch(TypedDict):
    field: str
    location: str
    snippet: str
    match_start: int
    match_end: int


class TaskSearchResult(TypedDict):
    matches: list[TaskSearchMatch]
    match_count: int


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
    base = os.path.abspath(task_dir)
    # Retain the validated boundary for this resolution only. The candidate
    # and all link/reparse checks remain fresh if a parent changes meanwhile.
    resolved_paths: dict[str, str | None] = {base: None}
    validate_task_directory(task_dir, _resolved_paths=resolved_paths)
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
    validate_workspace_file(candidate, base, label="Task payload", _resolved_paths=resolved_paths)
    return candidate


def _read_payload_text(handle: BinaryIO, path: str, max_bytes: int) -> str:
    raw = read_bounded_bytes(handle, max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"Task payload is too large (max {max_bytes} bytes): {path}")
    return raw.decode("utf-8")


def _read_text_limited(path: str, *, max_bytes: int = MAX_TASK_PAYLOAD_BYTES) -> str:
    with open(path, "rb") as handle:
        return _read_payload_text(handle, path, max_bytes)


def read_task_payload(
    task_dir: str, info: Dict[str, Any], *, config_view: bool = False,
) -> Tuple[str, DictConfig | Dict[str, Any], str, str]:
    """Return ``(task_kind, config, config_text, load_error)`` for one task."""

    snapshot = read_task_payload_snapshot(task_dir, info, config_view=config_view)
    return snapshot.task_kind, snapshot.config, snapshot.config_text, snapshot.load_error


def read_task_payload_snapshot(
    task_dir: str, info: Dict[str, Any], *, config_view: bool = False,
) -> TaskPayloadSnapshot:
    """Read task content and retain the opened file's identity before reading."""

    task_kind = normalize_task_kind(info.get("task_kind", info.get("config_mode")))
    config_file = resolve_task_config_file(info, task_kind, task_dir)
    try:
        config_path = resolve_task_payload_path(task_dir, config_file)
    except ValueError as exc:
        return TaskPayloadSnapshot(task_kind, _empty_config(), "", str(exc), None)

    if not os.path.exists(config_path):
        return TaskPayloadSnapshot(task_kind, _empty_config(), "", f"{config_file} is missing", None)

    signature = None
    try:
        with open(config_path, "rb") as handle:
            try:
                stat = os.fstat(handle.fileno())
                signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            except (AttributeError, OSError, ValueError):
                pass
            # A later file change must remain visible to the next refresh,
            # even if this read or parsing fails after obtaining the signature.
            text = _read_payload_text(handle, config_path, MAX_TASK_PAYLOAD_BYTES)
        if task_kind == TASK_KIND_SHELL:
            return TaskPayloadSnapshot(task_kind, _empty_config(), text, "", signature)
        parse = load_config_view_text if config_view else load_config_text
        parsed = parse(text)
        if not isinstance(parsed, DictConfig) and not (config_view and isinstance(parsed, dict)):
            raise ValueError(f"YAML root must be a mapping: {config_path}")
        return TaskPayloadSnapshot(task_kind, parsed, "", "", signature)
    except Exception as exc:
        return TaskPayloadSnapshot(task_kind, _empty_config(), "", str(exc), signature)


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


def _task_search_sources(task: Mapping[str, Any], search_field: str = "all", *, include_payload: bool = True):
    name = str(task.get("name", "") or "")
    if name and search_field in {"all", "name"}:
        yield "name", "", name

    notes = str(task.get("notes", "") or "")
    note_lines = notes.splitlines() if search_field in {"all", "notes"} else []
    for line_number, line in enumerate(note_lines, start=1):
        location = f"Line {line_number}" if len(note_lines) > 1 else ""
        yield "notes", location, line

    if search_field in {"all", "env"}:
        task_env = task.get("env") or {}
        if isinstance(task_env, Mapping):
            env_items = task_env.items()
        else:
            env_items = ()
        for key, value in env_items:
            for line in f"{key}={value}".splitlines():
                yield "env", str(key), line

    if not include_payload:
        return

    if normalize_task_kind(task.get("task_kind")) == TASK_KIND_SHELL:
        if search_field not in {"all", "script"}:
            return
        for line_number, line in enumerate(str(task.get("config_text", "") or "").splitlines(), start=1):
            yield "script", f"Line {line_number}", line
        return

    if search_field not in {"all", "config"}:
        return
    config = task.get("config", {}) or {}
    if not isinstance(config, (Mapping, DictConfig)):
        return
    for path, value in iter_config_fields(config):
        root_key = str(path[0])
        if root_key.startswith("_meta"):
            continue
        key_text = root_key if len(path) == 1 else ".".join(map(str, path))
        detail_lines = f"{key_text}: {value}".splitlines()
        for line in detail_lines:
            if line.strip():
                yield "config", key_text, line


def filter_tasks_by_search_field(
    tasks: list[_Task], query: str, status: str = "All", search_field: str = "all",
    *, matcher: SearchQuery | None = None, include_payload: bool = True,
) -> list[_Task]:
    """Filter metadata using the same sources as the displayed match previews."""
    candidates = filter_tasks(tasks, "", status)
    matcher = matcher or SearchQuery(query)
    if not matcher.needles:
        return candidates
    return [
        task for task in candidates
        if len(task_search_found(task, matcher, search_field, include_payload=include_payload)) == len(matcher.needles)
    ]


def task_search_found(
    task: Mapping[str, Any], matcher: SearchQuery, search_field: str = "all", *, include_payload: bool = True,
) -> set[str]:
    """Match cached text, optionally limiting uncached sources to metadata."""
    if matcher.plain and search_field == "all" and task.get("search_text"):
        # Preserve the cached metadata fast path, adding task-specific env values.
        text = task["search_text"] + "\n" + "\n".join(source for _, _, source in _task_search_sources(task, "env"))
        return matcher.found(text)
    found = set()
    for _, _, source in _task_search_sources(task, search_field, include_payload=include_payload):
        found.update(matcher.found(source))
        if len(found) == len(matcher.needles):
            break
    return found


def build_task_search_matches(
    task: Mapping[str, Any],
    query: str,
    *,
    limit: int = _TASK_SEARCH_MATCH_LIMIT,
    max_snippet_chars: int = _TASK_SEARCH_SNIPPET_CHARS,
) -> list[TaskSearchMatch]:
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
    search_field: str = "all",
    matcher: SearchQuery | None = None,
    limit: int = _TASK_SEARCH_MATCH_LIMIT,
    max_snippet_chars: int = _TASK_SEARCH_SNIPPET_CHARS,
) -> TaskSearchResult:
    """Return bounded contexts and the exact in-memory match count for one task."""

    matcher = matcher or SearchQuery(query)
    if not matcher.needles:
        return {"matches": [], "match_count": 0}

    safe_limit = max(0, int(limit))
    snippet_chars = max(32, int(max_snippet_chars))
    matches: list[TaskSearchMatch] = []
    match_count = 0
    for field, location, source in _task_search_sources(task, search_field):
        result = matcher.scan(source, max(0, safe_limit - len(matches)))
        match_count += result["match_count"]
        for start, end in result["spans"]:
            snippet, match_start, match_end = _build_task_search_snippet(source, None, start, end - start, snippet_chars)
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
