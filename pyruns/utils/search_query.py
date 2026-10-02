"""Shared literal/regular-expression search with bounded regex execution."""

from __future__ import annotations

from concurrent.futures import CancelledError
from dataclasses import dataclass
import re
from typing import TypedDict

import regex

REGEX_TIMEOUT_SECONDS = 0.2
DEFAULT_MAX_SEARCH_RESULTS = 20_000


@dataclass
class SearchBudget:
    """Count accepted matches and stop work once the requested limit is reached."""

    remaining: int | None = None
    limit_hit: bool = False

    def take(self, count: int) -> int:
        if self.remaining is None:
            return count
        accepted = min(count, self.remaining)
        self.remaining -= accepted
        if count and self.remaining == 0:
            self.limit_hit = True
        return accepted


def search_pattern(text: str, *, use_regex=False, whole_word=False) -> str:
    """Build the expression used by VS Code's createRegExp whole-word option."""
    pattern = text if use_regex else re.sub(r"([\\{}*+?|^$.\[\]()])", r"\\\1", text)
    if whole_word and pattern:
        if pattern[0].isascii() and (pattern[0].isalnum() or pattern[0] == "_"):
            pattern = r"\b" + pattern
        if pattern[-1].isascii() and (pattern[-1].isalnum() or pattern[-1] == "_"):
            pattern += r"\b"
    return pattern


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
        query = str(query or "").replace("\r\n", "\n")
        # VS Code's isMultilineRegexSource recognizes LF and unescaped n/r/W.
        self.multiline = "\n" in query or (use_regex and bool(re.search(r"(?<!\\)(?:\\\\)*\\[nrW]", query)))
        self.plain = not (match_case or whole_word or use_regex or self.multiline)
        self.raw_needles = {(query if use_regex else self.normalize(query)): query} if query else {}
        self.needles = tuple(self.raw_needles)
        # Let the external engine apply its own case folding. Lowercasing a
        # pattern first can change its codepoints (for example capital I-dot).
        self.cache_key = (tuple(self.raw_needles.values()), match_case, whole_word, use_regex)
        self.patterns = {}
        if whole_word or use_regex:
            try:
                for needle in self.needles:
                    pattern = search_pattern(self.raw_needles[needle], use_regex=use_regex, whole_word=whole_word)
                    if not use_regex:
                        pattern = self.normalize(pattern)
                    flags = regex.VERSION0 | regex.MULTILINE | (regex.IGNORECASE if use_regex and not match_case else 0)
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

    def scan(self, text: object, limit: int = 24, *, max_count: int | None = None) -> SearchScanResult:
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
                        if max_count is not None and count >= max_count:
                            break
                        found.add(needle)
                        count += 1
                        if kept < limit:
                            spans.append(match.span())
                            kept += 1
                else:
                    occurrences = normalized.count(needle)
                    if max_count is not None:
                        occurrences = min(occurrences, max_count)
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
