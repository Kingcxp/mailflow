from __future__ import annotations

from mailflow.domain import MailRecord
from mailflow.ocr import searchable_mail_text
from mailflow_testkit.fakes import make_mail
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


def test_mail_body_and_image_text_preserve_case_sensitive_search() -> None:
    record = MailRecord(
        record_id="search",
        mail=make_mail(body_text="", body_html="<b>Room</b> Y908").model_copy(
            update={"image_text": "Research Meeting"}
        ),
    )
    text = searchable_mail_text(record.mail)
    assert build_search_matcher("Room Y908", case_sensitive=True).matches(text)
    assert build_search_matcher("Research", case_sensitive=True).matches(text)
    assert not build_search_matcher("research", case_sensitive=True).matches(text)
