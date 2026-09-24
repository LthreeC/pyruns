"""On-demand, bounded-memory search of persisted task logs."""

from __future__ import annotations

import codecs
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
from concurrent.futures import CancelledError
from dataclasses import dataclass
from types import MappingProxyType

from pyruns.utils.info_io import get_log_options
from pyruns.utils.log_io import _log_decode_candidates, log_file_identity
from pyruns.utils.search_query import SearchQuery, SearchQueryError, normalized_search_with_positions
from pyruns.utils.task_files import _build_task_search_snippet

_CHUNK_CHARS = 16 * 1024
_PREVIEW_LIMIT = 24
_MAX_PATTERN_LINE_CHARS = 8 * 1024 * 1024
_CACHE_QUERIES = 2
_CACHE_FILES_PER_QUERY = 1024
_CACHE_MISSES_PER_QUERY = 8192
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_INCOMPLETE_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*|\][^\x07\x1b]*)?$")
_UNDECODABLE = re.compile("[\udc80-\udcff]")


def _case_context(text, previous):
    """Keep the preceding Unicode case context without retaining old log text."""
    text = _INCOMPLETE_ANSI.sub("", _ANSI.sub("", text))
    # A synthetic sigma uses exactly this Python version's Unicode rules.
    return "A" if (previous + text + "Σ").lower().endswith("ς") else "0"


def _next_character_is_cased(handle, size, cancelled, pending_ansi=""):
    """Resolve a trailing sigma with bounded lookahead, restoring the reader."""
    position = handle.tell()
    state = "text"
    chunk = pending_ansi
    try:
        while chunk or handle.tell() < size:
            if cancelled.is_set():
                raise CancelledError()
            if not chunk:
                chunk = handle.read(min(_CHUNK_CHARS, size - handle.tell()))
                if not chunk:
                    break
            for char in chunk:
                if state == "text":
                    if char == "\x1b":
                        state = "escape"
                    elif ("AΣ" + char).lower().startswith("aσ"):
                        return True
                    elif ("AΣ" + char + "A").lower().startswith("aς"):
                        return False
                    # Otherwise this character is ignored by contextual casing.
                elif state == "escape":
                    if char == "[":
                        state = "csi"
                    elif char == "]":
                        state = "osc"
                    else:
                        return False
                elif state in ("csi", "csi_intermediate"):
                    if "@" <= char <= "~":
                        state = "text"
                    elif " " <= char <= "/":
                        state = "csi_intermediate"
                    elif state != "csi" or not "0" <= char <= "?":
                        return False
                elif state == "osc":
                    if char == "\x07":
                        state = "text"
                    elif char == "\x1b":
                        state = "osc_escape"
                elif char == "\\":
                    state = "text"
                else:
                    return False
            chunk = ""
        return False
    finally:
        handle.seek(position)


def _encoding(path, offset=0):
    with open(path, "rb") as handle:
        handle.seek(offset)
        sample = handle.read(64 * 1024)
    for encoding in _log_decode_candidates():
        try:
            codecs.getincrementaldecoder(encoding)().decode(sample, final=False)
            # Ordinary UTF-8 keeps the BOM in byte-offset calculations.
            return "utf-8" if encoding == "utf-8-sig" else encoding
        except (LookupError, UnicodeDecodeError):
            continue
    return "utf-8"


@dataclass(frozen=True)
class _MissCacheSnapshot:
    query_key: tuple
    signatures: Mapping[str, tuple]


class LogSearch:
    """Cache only bounded match contexts; never store log contents or write logs."""

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

    def snapshot_misses(self, matcher):
        """Keep bounded negative signatures stable during one workspace scan."""
        with self._lock:
            cached = self._cache.get(matcher.cache_key)
            signatures = {path: entry[0] for path, entry in cached[1].items()} if cached else {}
        return _MissCacheSnapshot(matcher.cache_key, MappingProxyType(signatures))

    def search(self, task_dir, query, cancelled, matcher=None, *, miss_snapshot=None):
        matcher = matcher or SearchQuery(query)
        needles = matcher.needles
        known_misses = (
            miss_snapshot.signatures
            if miss_snapshot is not None and miss_snapshot.query_key == matcher.cache_key else None
        )
        result = {"matches": [], "match_count": 0, "found": set(), "errors": []}
        try:
            options = get_log_options(task_dir)
        except OSError:
            result["errors"].append("Could not list task logs")
            return result
        for name, path in options.items():
            if cancelled.is_set():
                raise CancelledError()
            try:
                stat = os.stat(path)
                signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                with self._lock:
                    matches, misses = self._query_cache(matcher.cache_key)
                    entry = matches.get(path)
                    if entry is None:
                        entry = misses.get(path)
                        if entry is not None:
                            misses.move_to_end(path)
                    cached = entry[1] if entry is not None and entry[0] == signature else None
                if cached is None:
                    # A long scan may evict its own later entries from the LRU.
                    # Reuse captured misses only after checking the current file.
                    if known_misses is not None and known_misses.get(path) == signature:
                        continue
                    cached = (
                        self._search_file(path, name, stat.st_size, needles, cancelled, match_case=matcher.match_case)
                        if not matcher.patterns else self._search_file_patterns(path, name, stat.st_size, matcher, cancelled)
                    )
                    with self._lock:
                        matches, misses = self._query_cache(matcher.cache_key)
                        if cached["match_count"]:
                            misses.pop(path, None)
                            if path in matches or len(matches) < _CACHE_FILES_PER_QUERY:
                                matches[path] = (signature, cached)
                        else:
                            matches.pop(path, None)
                            misses[path] = (signature, cached)
                            misses.move_to_end(path)
                            while len(misses) > _CACHE_MISSES_PER_QUERY:
                                misses.popitem(last=False)
                result["found"].update(cached["found"])
                result["match_count"] += cached["match_count"]
                result["matches"].extend(cached["matches"][:max(0, _PREVIEW_LIMIT - len(result["matches"]))])
            except OSError:
                result["errors"].append(f"Could not read {name}")
        return result

    @staticmethod
    def _search_file_patterns(path, name, size, matcher, cancelled, encoding_hint=None):
        """Evaluate complete lines so anchors, word boundaries and greedy matches are exact."""
        result = {"matches": [], "match_count": 0, "found": set()}
        encoding = encoding_hint or _encoding(path)
        identity = log_file_identity(path)
        line = 0
        next_offset = 0
        with open(path, encoding=encoding, errors="surrogateescape", newline="\n") as handle:
            while next_offset < size:
                if cancelled.is_set():
                    raise CancelledError()
                byte_offset = next_offset
                raw = handle.readline(min(_MAX_PATTERN_LINE_CHARS + 1, size - byte_offset))
                if not raw:
                    break
                next_offset += len(raw.encode(encoding, errors="surrogateescape"))
                if len(raw) > _MAX_PATTERN_LINE_CHARS:
                    raise SearchQueryError(
                        f"{name}: a line exceeds 8 Mi characters. Disable whole-word and regex matching for this log."
                    )
                line += 1
                undecodable = _UNDECODABLE.search(raw) if encoding_hint is None else None
                if undecodable:
                    invalid_offset = byte_offset + len(raw[:undecodable.start()].encode(encoding, errors="surrogateescape"))
                    detected = _encoding(path, invalid_offset)
                    if detected != encoding:
                        return LogSearch._search_file_patterns(path, name, size, matcher, cancelled, detected)
                display = _UNDECODABLE.sub("\ufffd", _INCOMPLETE_ANSI.sub("", _ANSI.sub("", raw))).rstrip("\r\n")
                matches = matcher.scan(display, max(0, _PREVIEW_LIMIT - len(result["matches"])))
                result["found"].update(matches["found"])
                result["match_count"] += matches["match_count"]
                for start, end in matches["spans"]:
                    raw_start = start
                    for escape in _ANSI.finditer(raw):
                        if escape.start() > raw_start:
                            break
                        raw_start += escape.end() - escape.start()
                    snippet, match_start, match_end = _build_task_search_snippet(display, None, start, end - start, 180)
                    delta = len(raw[:raw_start].encode(encoding, errors="surrogateescape"))
                    result["matches"].append({
                        "field": "log", "location": f"{name}:{line}", "log_file": name,
                        "line": line, "offset": max(0, byte_offset + delta - 256),
                        "log_identity": identity, "snippet": snippet,
                        "match_start": match_start, "match_end": match_end,
                    })
        return result

    @staticmethod
    def _search_file(path, name, size, needles, cancelled, encoding_hint=None, *, match_case=False):
        result = {"matches": [], "match_count": 0, "found": set()}
        normalize = SearchQuery("", match_case=match_case).normalize
        contextual_case = not match_case and any("σ" in needle or "ς" in needle for needle in needles)
        case_prefix = "0"
        overlap = max(8192, max(map(len, needles), default=0) * 8)
        encoding = encoding_hint or _encoding(path)
        identity = log_file_identity(path)
        carry = ""
        line = 1
        char_offset = 0
        last_end = dict.fromkeys(needles, 0)
        # TextIO decodes incrementally: chunk boundaries never split a codepoint.
        with open(path, encoding=encoding, errors="surrogateescape", newline="\n") as handle:
            while handle.tell() < size:
                if cancelled.is_set():
                    raise CancelledError()
                byte_offset = handle.tell()
                chunk = handle.read(min(_CHUNK_CHARS, size - byte_offset))
                if not chunk:
                    break
                if encoding_hint is None and _UNDECODABLE.search(chunk):
                    # A Windows log may have a long ASCII header before its
                    # first locale-encoded message. Retry once in that encoding.
                    detected = _encoding(path, byte_offset)
                    if detected != encoding:
                        return LogSearch._search_file(path, name, size, needles, cancelled, detected, match_case=match_case)
                raw = carry + chunk
                base = char_offset - len(carry)
                complete_end = raw.rfind("\n") + 1
                if len(result["matches"]) >= _PREVIEW_LIMIT and complete_end:
                    # Once previews are filled, count complete lines in C rather
                    # than mapping millions of occurrences back to characters.
                    # Queries never span LF, so the next chunk only needs the
                    # unfinished line; long lines still use the overlap below.
                    complete = raw[:complete_end]
                    for needle in needles:
                        unread_start = max(0, last_end[needle] - base)
                        unread = complete[unread_start:]
                        display = _UNDECODABLE.sub("\ufffd", _ANSI.sub("", unread))
                        prefix = _case_context(complete[:unread_start], case_prefix) if contextual_case else ""
                        count = normalize(prefix + display)[len(prefix):].count(needle)
                        if count:
                            result["found"].add(needle)
                            result["match_count"] += count
                        last_end[needle] = base + complete_end
                    raw = raw[complete_end:]
                    base += complete_end
                    case_prefix = "0"
                clean = _ANSI.sub("", raw)
                incomplete_ansi = _INCOMPLETE_ANSI.search(clean)
                display = clean[:incomplete_ansi.start()] if incomplete_ansi else clean
                display = _UNDECODABLE.sub("\ufffd", display)
                normalized = normalize(display)
                if contextual_case and "Σ" in display:
                    normalized = normalize(case_prefix + display)[1:]
                    continued = normalize(case_prefix + display + "A")[1:-1]
                    if continued != normalized and _next_character_is_cased(
                        handle, size, cancelled,
                        incomplete_ansi.group() if incomplete_ansi else "",
                    ):
                        normalized = continued
                hits = [needle for needle in needles if needle in normalized]
                if hits:
                    positions = None
                    if not (display.isascii() and len(normalized) == len(display)):
                        _, positions = normalized_search_with_positions(display, match_case)
                    raw_positions = None
                    if display != raw:
                        raw_positions = []
                        start = 0
                        for escape in _ANSI.finditer(raw):
                            raw_positions.extend(range(start, escape.start()))
                            start = escape.end()
                        raw_positions.extend(range(start, len(raw)))
                    for needle in hits:
                        start = 0
                        while True:
                            index = normalized.find(needle, start)
                            if index < 0:
                                break
                            end = index + len(needle)
                            source_start = positions[index] if positions is not None else index
                            source_end = positions[end - 1] if positions is not None else end - 1
                            raw_start = raw_positions[source_start] if raw_positions is not None else source_start
                            raw_end = (raw_positions[source_end] if raw_positions is not None else source_end) + 1
                            if base + raw_start < last_end[needle]:
                                start = index + 1
                                continue
                            last_end[needle] = base + raw_end
                            result["found"].add(needle)
                            result["match_count"] += 1
                            if len(result["matches"]) < _PREVIEW_LIMIT:
                                line_start = display.rfind("\n", 0, source_start) + 1
                                line_end = display.find("\n", source_end)
                                if line_end < 0:
                                    line_end = len(display)
                                snippet, match_start, match_end = _build_task_search_snippet(
                                    display[line_start:line_end], None, source_start - line_start,
                                    source_end - source_start + 1, 180,
                                )
                                delta = (len(raw[:raw_start].encode(encoding, errors="surrogateescape"))
                                         - len(carry.encode(encoding, errors="surrogateescape")))
                                match_line = line + raw.count("\n", 0, raw_start)
                                result["matches"].append({
                                    "field": "log", "location": f"{name}:{match_line}", "log_file": name,
                                    "line": match_line, "offset": max(0, byte_offset + delta - 256),
                                    "log_identity": identity, "snippet": snippet,
                                    "match_start": match_start, "match_end": match_end,
                                })
                            start = end
                char_offset += len(chunk)
                line += chunk.count("\n")
                carry_start = max(raw.rfind("\n") + 1, len(raw) - overlap)
                if contextual_case:
                    for escape in _ANSI.finditer(raw):
                        if escape.start() < carry_start < escape.end():
                            carry_start = escape.start()
                            break
                    case_prefix = _case_context(raw[:carry_start], case_prefix)
                carry = raw[carry_start:]
        return result
