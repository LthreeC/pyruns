"""Shared literal/regular-expression search with bounded regex execution."""

from __future__ import annotations

import re
from array import array
from concurrent.futures import CancelledError

import regex

REGEX_TIMEOUT_SECONDS = 0.2
_COLON_SPACES = re.compile(r"[^\S\r\n]*:[^\S\r\n]*")


class SearchQueryError(ValueError):
    """A search expression is invalid or cannot be evaluated within its limits."""


def normalized_search_with_positions(text, match_case=False, end=None):
    """Keep source offsets, optionally only for a prefix of normalized text."""
    positions = array("I")
    index = 0
    while index < len(text):
        char = text[index]
        if char == ":":
            while positions and text[positions[-1]].isspace() and text[positions[-1]] not in "\r\n":
                positions.pop()
            positions.append(index)
            index += 1
            while index < len(text) and text[index].isspace() and text[index] not in "\r\n":
                index += 1
            continue
        for _ in char if match_case else char.lower():
            positions.append(index)
        index += 1
        # Whitespace can still be removed by a following colon. Once a
        # non-whitespace character is reached, all earlier positions are stable.
        if end is not None and len(positions) >= end and not char.isspace():
            break
    # Lowercase the whole string to preserve contextual casing (e.g. Greek sigma).
    return _COLON_SPACES.sub(":", text if match_case else text.lower()), positions


class SearchQuery:
    def __init__(self, query, *, match_case=False, whole_word=False, use_regex=False, cancelled=None):
        self.cancelled = cancelled
        self.match_case = match_case
        self.whole_word = whole_word
        self.use_regex = use_regex
        self.plain = not (match_case or whole_word or use_regex)
        self.needles = tuple(dict.fromkeys(
            line if use_regex else self.normalize(line.strip())
            for line in str(query or "").split("\n") if line.strip()
        ))
        self.cache_key = (self.needles, match_case, whole_word, use_regex)
        self.patterns = {}
        if whole_word or use_regex:
            try:
                for needle in self.needles:
                    pattern = needle if use_regex else regex.escape(needle)
                    if whole_word:
                        pattern = rf"(?<!\w)(?:{pattern})(?!\w)"
                    flags = regex.VERSION0 | (regex.IGNORECASE if use_regex and not match_case else 0)
                    self.patterns[needle] = regex.compile(pattern, flags)
            except (regex.error, RecursionError, OverflowError) as exc:
                raise SearchQueryError(f"Invalid regular expression: {exc}") from exc

    def normalize(self, text):
        if self.use_regex:
            return text
        return _COLON_SPACES.sub(":", text if self.match_case else text.lower())

    def found(self, text):
        if self.cancelled is not None and self.cancelled.is_set():
            raise CancelledError()
        text = self.normalize(str(text or ""))
        try:
            return {
                needle for needle in self.needles
                if (self.patterns[needle].search(text, timeout=REGEX_TIMEOUT_SECONDS, concurrent=True)
                    if self.patterns else needle in text)
            }
        except TimeoutError as exc:
            raise SearchQueryError("Search pattern took too long. Simplify the regular expression.") from exc

    def scan(self, text, limit=24):
        """Count all matches, retaining only bounded original-character spans."""
        if self.cancelled is not None and self.cancelled.is_set():
            raise CancelledError()
        text = str(text or "")
        normalized = self.normalize(text)
        count, found, spans = 0, set(), []
        try:
            for needle in self.needles:
                if self.patterns:
                    kept = 0
                    for match in self.patterns[needle].finditer(normalized, timeout=REGEX_TIMEOUT_SECONDS, concurrent=True):
                        found.add(needle)
                        count += 1
                        if kept < limit:
                            spans.append(match.span())
                            kept += 1
                else:
                    occurrences = normalized.count(needle)
                    if not occurrences:
                        continue
                    found.add(needle)
                    count += occurrences
                    start = 0
                    for _ in range(min(occurrences, limit)):
                        start = normalized.find(needle, start)
                        end = start + len(needle)
                        spans.append((start, end))
                        start = end
        except TimeoutError as exc:
            raise SearchQueryError("Search pattern took too long. Simplify the regular expression.") from exc
        spans = sorted(spans)[:limit]
        if spans and not self.use_regex and not (
            len(normalized) == len(text)
            and (self.match_case or text.isascii() or len(text.lower()) == len(text))
        ):
            _, positions = normalized_search_with_positions(text, self.match_case, end=max(end for _, end in spans))
            spans = [(positions[start], positions[end - 1] + 1) for start, end in spans]
        return {"match_count": count, "found": found, "spans": spans}
