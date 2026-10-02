"""Shared literal/regular-expression search with bounded regex execution."""

from __future__ import annotations

from concurrent.futures import CancelledError
from typing import TypedDict

import regex

REGEX_TIMEOUT_SECONDS = 0.2


class SearchScanResult(TypedDict):
    match_count: int
    found: set[str]
    spans: list[tuple[int, int]]


class SearchQueryError(ValueError):
    """A search expression is invalid or cannot be evaluated within its limits."""


def fold_search_case(text: str) -> str:
    """Use one-codepoint Unicode case folding, as ripgrep does for literals."""
    if text.isascii():
        return text.lower()
    replacements = {}
    for char in set(text):
        folded = char.casefold()
        if len(folded) != 1:
            # Full folding expands e.g. sharp S to "ss". Simple folding only
            # uses its lowercase mapping when that still has one codepoint.
            folded = char.lower()
        replacements[ord(char)] = folded if len(folded) == 1 else char
    return text.translate(replacements)


class SearchQuery:
    def __init__(self, query, *, match_case=False, whole_word=False, use_regex=False, cancelled=None):
        self.cancelled = cancelled
        self.match_case = match_case
        self.whole_word = whole_word
        self.use_regex = use_regex
        self.plain = not (match_case or whole_word or use_regex)
        self.raw_needles = {
            (line if use_regex else self.normalize(line)): line
            for line in str(query or "").split("\n") if line.strip()
        }
        self.needles = tuple(self.raw_needles)
        # Let the external engine apply its own case folding. Lowercasing a
        # pattern first can change its codepoints (for example capital I-dot).
        self.cache_key = (tuple(self.raw_needles.values()), match_case, whole_word, use_regex)
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

    def normalize(self, text: str) -> str:
        if self.use_regex:
            return text
        return text if self.match_case else fold_search_case(text)

    def found(self, text: object) -> set[str]:
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

    def scan(self, text: object, limit: int = 24) -> SearchScanResult:
        """Count all matches, retaining only bounded original-character spans."""
        if self.cancelled is not None and self.cancelled.is_set():
            raise CancelledError()
        text = str(text or "")
        normalized = self.normalize(text)
        count = 0
        found: set[str] = set()
        spans: list[tuple[int, int]] = []
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
        return {"match_count": count, "found": found, "spans": spans}
