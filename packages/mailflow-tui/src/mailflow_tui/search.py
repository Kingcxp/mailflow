"""Shared search matching semantics for TUI filter inputs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from re import Pattern


@dataclass(frozen=True)
class SearchMatcher:
    query: str
    pattern: Pattern[str] | None = None
    error: str | None = None

    def matches(self, text: str) -> bool:
        return not self.query or (
            self.error is None
            and self.pattern is not None
            and self.pattern.search(text) is not None
        )


def build_search_matcher(
    query: str, *, regex: bool = False, case_sensitive: bool = False
) -> SearchMatcher:
    query = query.strip()
    if not query:
        return SearchMatcher(query)
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return SearchMatcher(query, re.compile(query if regex else re.escape(query), flags))
    except re.error as exc:
        return SearchMatcher(query, error=str(exc))
