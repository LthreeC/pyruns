"""Batched ripgrep search of task logs with bounded result caches."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import threading
from collections import OrderedDict
from concurrent.futures import CancelledError
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, distribution
from typing import TypedDict

from pyruns.utils.info_io import get_log_entries
from pyruns.utils.process_utils import hidden_subprocess_kwargs
from pyruns.utils.search_query import SearchQuery, SearchQueryError
from pyruns.utils.task_files import TaskSearchMatch, _build_task_search_snippet

_PREVIEW_LIMIT = 24
_CACHE_QUERIES = 2
_CACHE_FILES_PER_QUERY = 1024
_CACHE_MISSES_PER_QUERY = 8192
_ARG_BYTES = 24_000 if os.name == "nt" else 256_000
_MAX_JSON_BYTES = 16 * 1024 * 1024
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


class LogSearchMatch(TaskSearchMatch):
    log_file: str
    line: int
    offset: int
    log_identity: str


class _LogFileResult(TypedDict):
    matches: list[LogSearchMatch]
    match_count: int
    found: set[str]


class LogSearchResult(_LogFileResult):
    errors: list[str]


def _empty_result() -> LogSearchResult:
    return {"matches": [], "match_count": 0, "found": set(), "errors": []}


@lru_cache(maxsize=1)
def ripgrep_path() -> str:
    """Locate the pinned dependency even when the environment is not on PATH."""
    try:
        package = distribution("ripgrep-bin")
        for entry in package.files or ():
            if entry.name in {"rg", "rg.exe"}:
                path = os.path.abspath(str(package.locate_file(entry)))
                if os.path.isfile(path):
                    return path
    except PackageNotFoundError:
        pass
    raise SearchQueryError("ripgrep-bin is missing. Reinstall pyruns with its dependencies to search logs.")


def _json_bytes(value: dict) -> bytes:
    return value["text"].encode("utf-8") if "text" in value else base64.b64decode(value["bytes"])


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _batches(paths, cwd, pattern):
    # Leave room for flags and quoting. Relative paths avoid excessive launches
    # in large workspaces and stay within the Windows command-line limit.
    size = len(pattern.encode("utf-8")) * 2 + 4096
    batch = []
    for path in paths:
        argument = os.path.relpath(path, cwd)
        cost = len(argument.encode("utf-8")) * 2 + 4
        if batch and size + cost > _ARG_BYTES:
            yield batch
            batch, size = [], len(pattern.encode("utf-8")) * 2 + 4096
        batch.append(argument)
        size += cost
    if batch:
        yield batch


def _ripgrep_events(paths, cwd, needle, matcher, cancelled):
    """Stream JSON while a watcher can interrupt even a blocked pipe/NFS read."""
    if cancelled.is_set():
        raise CancelledError()
    command = [
        ripgrep_path(), "--no-config", "--json", "--text", "--encoding", "none",
        "--no-mmap", "--no-ignore", "--hidden", "--no-follow", "--crlf", "--threads", "4",
        "--case-sensitive" if matcher.match_case else "--ignore-case",
        "--engine=auto" if matcher.use_regex else "--fixed-strings",
    ]
    if matcher.whole_word:
        command.append("--word-regexp")
    command.extend(["--regexp", needle, "--", *paths])
    finished = threading.Event()
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=errors, **hidden_subprocess_kwargs(),
            )
        except OSError as exc:
            raise SearchQueryError(f"Could not start ripgrep: {exc}") from exc

        def watch_cancel():
            while not finished.wait(0.05):
                if cancelled.is_set():
                    try:
                        process.kill()
                    except OSError:
                        pass
                    return

        watcher = threading.Thread(target=watch_cancel, name="pyruns-search-cancel", daemon=True)
        watcher.start()
        try:
            assert process.stdout is not None
            while True:
                line = process.stdout.readline(_MAX_JSON_BYTES)
                if cancelled.is_set():
                    raise CancelledError()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    raise SearchQueryError("A matching log line exceeds the 16 MiB search result limit.")
                event = json.loads(line)
                if event["type"] == "match":
                    yield event
            code = process.wait()
            if cancelled.is_set():
                raise CancelledError()
            if code not in (0, 1):
                errors.seek(0)
                message = errors.read(32 * 1024).decode("utf-8", errors="replace").strip()
                if not message or "regex parse error" in message or "error compiling pattern" in message:
                    raise SearchQueryError(message or f"ripgrep exited with code {code}")
                yield {"type": "error", "data": message}
        finally:
            finished.set()
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
            watcher.join()


def _append_match(result, data, name, info):
    submatches = data["submatches"]
    result["match_count"] += len(submatches)
    remaining = _PREVIEW_LIMIT - len(result["matches"])
    if remaining <= 0:
        return
    raw = _json_bytes(data["lines"])
    display = _ANSI.sub("", raw.decode("utf-8", errors="replace")).rstrip("\r\n")
    for match in submatches[:remaining]:
        start = len(_ANSI.sub("", raw[:match["start"]].decode("utf-8", errors="replace")))
        end = len(_ANSI.sub("", raw[:match["end"]].decode("utf-8", errors="replace")))
        start, end = min(start, len(display)), min(end, len(display))
        snippet, match_start, match_end = _build_task_search_snippet(display, None, start, end - start, 180)
        line = data["line_number"]
        result["matches"].append({
            "field": "log", "location": f"{name}:{line}", "log_file": name,
            "line": line, "offset": max(0, data["absolute_offset"] + match["start"] - 256),
            "log_identity": f"{info.st_dev:x}:{info.st_ino:x}", "snippet": snippet,
            "match_start": match_start, "match_end": match_end,
        })


class LogSearch:
    """Cache match contexts and signatures, never log contents or search indexes."""

    def __init__(self):
        self._cache = OrderedDict()
        self._lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(2)

    def _query_cache(self, cache_key):
        cached_files = self._cache.get(cache_key)
        if cached_files is None:
            cached_files = ({}, OrderedDict())
            self._cache[cache_key] = cached_files
            if len(self._cache) > _CACHE_QUERIES:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(cache_key)
        return cached_files

    def search(self, task_dir, query, cancelled, matcher=None) -> LogSearchResult:
        return self.search_many([task_dir], query, cancelled, matcher)[task_dir]

    def search_many(self, task_dirs, query, cancelled, matcher=None) -> dict[str, LogSearchResult]:
        """Search uncached files in batches, preserving task-level keyword AND."""
        matcher = matcher or SearchQuery(query)
        results = {task_dir: _empty_result() for task_dir in task_dirs}
        if cancelled.is_set():
            raise CancelledError()
        if not matcher.needles or not results:
            return results
        with self._lock:
            matches, misses = self._query_cache(matcher.cache_key)
            snapshot = {**misses, **matches}
        files, contents, pending = {}, {}, []
        for task_dir in results:
            if cancelled.is_set():
                raise CancelledError()
            try:
                entries = get_log_entries(task_dir)
            except OSError:
                results[task_dir]["errors"].append("Could not list task logs")
                continue
            for name, (path, info) in entries.items():
                files[path] = (task_dir, name, info)
                cached = snapshot.get(path)
                if cached is not None and cached[0] == _signature(info):
                    contents[path] = cached[1]
                else:
                    pending.append(path)
                    contents[path] = _empty_result()
        failed = set()
        if pending:
            cwd = os.path.dirname(os.path.commonpath(pending))
            for needle in matcher.needles:
                # Separate passes preserve overlapping keywords and AND across
                # different log files or metadata fields in the same task.
                pattern = matcher.raw_needles[needle]
                for batch in _batches(pending, cwd, pattern):
                    for event in _ripgrep_events(batch, cwd, pattern, matcher, cancelled):
                        if event["type"] == "error":
                            failed.update(os.path.normpath(os.path.join(cwd, path)) for path in batch)
                            task_dir = files[os.path.normpath(os.path.join(cwd, batch[0]))][0]
                            results[task_dir]["errors"].append(event["data"])
                            continue
                        data = event["data"]
                        path = os.path.normpath(os.path.join(cwd, os.fsdecode(_json_bytes(data["path"]))))
                        _, name, info = files[path]
                        contents[path]["found"].add(needle)
                        _append_match(contents[path], data, name, info)
        if cancelled.is_set():
            raise CancelledError()
        with self._lock:
            matches, misses = self._query_cache(matcher.cache_key)
            for path in pending:
                if path in failed:
                    matches.pop(path, None)
                    misses.pop(path, None)
                    continue
                cached = contents[path]
                entry = (_signature(files[path][2]), cached)
                if cached["match_count"]:
                    misses.pop(path, None)
                    if path in matches or len(matches) < _CACHE_FILES_PER_QUERY:
                        matches[path] = entry
                else:
                    matches.pop(path, None)
                    misses[path] = entry
                    misses.move_to_end(path)
                    while len(misses) > _CACHE_MISSES_PER_QUERY:
                        misses.popitem(last=False)
        # Stable queue/run1/run2/.../error previews, regardless of worker order.
        for path, (task_dir, _, _) in files.items():
            cached = contents[path]
            result = results[task_dir]
            result["found"].update(cached["found"])
            result["match_count"] += cached["match_count"]
            result["matches"].extend(cached["matches"][:max(0, _PREVIEW_LIMIT - len(result["matches"]))])
        return results
