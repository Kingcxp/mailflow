from __future__ import annotations

from mailflow_tui.search import build_search_matcher


def test_literal_search_is_case_insensitive_by_default() -> None:
    matcher = build_search_matcher("Mail")
    assert matcher.matches("new MAIL message")
    assert not matcher.matches("message")


def test_regex_and_case_sensitive_modes() -> None:
    regex = build_search_matcher(r"m[a-z]+l", regex=True)
    assert regex.matches("Mail")
    assert regex.matches("MAIL")
    sensitive_regex = build_search_matcher(r"m[a-z]+l", regex=True, case_sensitive=True)
    assert sensitive_regex.matches("mail")
    assert not sensitive_regex.matches("MAIL")


def test_invalid_regex_is_reported_without_raising() -> None:
    matcher = build_search_matcher("[", regex=True)
    assert matcher.error
    assert not matcher.matches("anything")


def test_empty_query_matches_all() -> None:
    matcher = build_search_matcher("")
    assert matcher.error is None
    assert matcher.matches("anything")
