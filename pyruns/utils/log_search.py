"""On-demand, bounded-memory search of persisted task logs."""

from __future__ import annotations

import codecs
import os
import re
import threading
from collections import OrderedDict
from concurrent.futures import CancelledError

from pyruns.utils.info_io import get_log_options
from pyruns.utils.log_io import _log_decode_candidates, log_file_identity
from pyruns.utils.sort_utils import normalize_task_search_text, task_search_needles
from pyruns.utils.task_files import _build_task_search_snippet, _normalized_search_with_positions

_CHUNK_CHARS = 16 * 1024
_PREVIEW_LIMIT = 24
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_INCOMPLETE_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*|\][^\x07\x1b]*)?$")
_UNDECODABLE = re.compile("[\udc80-\udcff]")


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


class LogSearch:
    """Cache only bounded match contexts; never store log contents or write logs."""

    def __init__(self):
        self._cache = OrderedDict()
        self._lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(2)

    def search(self, task_dir, query, cancelled):
        needles = task_search_needles(query)
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
                key = (path, signature, tuple(needles))
                with self._lock:
                    cached = self._cache.get(key)
                    if cached is not None:
                        self._cache.move_to_end(key)
                if cached is None:
                    cached = self._search_file(path, name, stat.st_size, needles, cancelled)
                    with self._lock:
                        self._cache[key] = cached
                        while len(self._cache) > 128:
                            self._cache.popitem(last=False)
                result["found"].update(cached["found"])
                result["match_count"] += cached["match_count"]
                result["matches"].extend(cached["matches"][:max(0, _PREVIEW_LIMIT - len(result["matches"]))])
            except OSError:
                result["errors"].append(f"Could not read {name}")
        return result

    @staticmethod
    def _search_file(path, name, size, needles, cancelled, encoding_hint=None):
        result = {"matches": [], "match_count": 0, "found": set()}
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
                        return LogSearch._search_file(path, name, size, needles, cancelled, detected)
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
                        unread = complete[max(0, last_end[needle] - base):]
                        count = normalize_task_search_text(_UNDECODABLE.sub("\ufffd", _ANSI.sub("", unread))).count(needle)
                        if count:
                            result["found"].add(needle)
                            result["match_count"] += count
                        last_end[needle] = base + complete_end
                    raw = raw[complete_end:]
                    base += complete_end
                display = _INCOMPLETE_ANSI.sub("", _ANSI.sub("", raw))
                display = _UNDECODABLE.sub("\ufffd", display)
                normalized = normalize_task_search_text(display)
                hits = [needle for needle in needles if needle in normalized]
                if hits:
                    positions = None
                    if not (display.isascii() and normalized == display.lower()):
                        normalized, positions = _normalized_search_with_positions(display)
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
                carry = raw[max(raw.rfind("\n") + 1, len(raw) - overlap):]
        return result
