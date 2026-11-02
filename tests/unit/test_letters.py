"""Reply markup and localized letters through their public output."""

from __future__ import annotations

from datetime import date

import pytest
from mailflow.letters import build_letter, html_to_text, markup_to_html


def test_reply_markup_round_trip_preserves_structure_and_escapes_text() -> None:
    html = markup_to_html(
        "**bold** and *italic* and <tag>\nline two\n\n"
        "<center>title</center> <right>**signed**\nsecond line</right>"
    )
    assert "<b>bold</b>" in html and "<i>italic</i>" in html
    assert "&lt;tag&gt;<br/>line two" in html
    assert '<div style="text-align:center">title</div>' in html
    assert '<div style="text-align:right"><b>signed</b><br/>second line</div>' in html
    plain = html_to_text(html)
    assert "bold and italic and <tag>" in plain
    assert "signed\nsecond line" in plain
    assert "<b>" not in plain and "<div" not in plain
    assert markup_to_html(html) == html


@pytest.mark.parametrize(
    ("language", "recipient", "opening", "body", "signature", "formatted_date"),
    [
        ("cn", "张先生", "尊敬的张先生：", "感谢来信。", "李四", "2026年8月16日"),
        ("en", "John", "Dear John,", "Thank you.", "Jane", "August 16, 2026"),
    ],
    ids=["zh", "en"],
)
def test_letter_contains_localized_content_and_editable_defaults(
    language: str, recipient: str, opening: str, body: str, signature: str, formatted_date: str
) -> None:
    html = build_letter(
        language,
        recipient=recipient,
        today=date(2026, 8, 16),
        opening=opening,
        body=body,
        signature=signature,
    )
    assert f"<p>{opening}</p>" in html and f"<p>{body}</p>" in html
    assert 'style="text-align:right"' in html
    assert signature in html and formatted_date in html
    skeleton = html_to_text(build_letter(language, recipient=recipient, today=date(2026, 8, 16)))
    assert recipient in skeleton and formatted_date in skeleton
