"""Letter templates and the lightweight markup dialect (mail replies)."""

from __future__ import annotations

from datetime import date

import pytest
from mailflow.letters import build_letter, html_to_text, markup_to_html


class TestMarkupToHtml:
    def test_bold_italic_and_escaping(self) -> None:
        html = markup_to_html("**bold** and *italic* and <tag>")
        assert "<p><b>bold</b> and <i>italic</i> and &lt;tag&gt;</p>" in html

    def test_paragraphs_and_line_breaks(self) -> None:
        html = markup_to_html("line one\nline two\n\nsecond paragraph")
        assert html == "<p>line one<br/>line two</p>\n<p>second paragraph</p>"

    def test_alignment_blocks(self) -> None:
        html = markup_to_html("left <right>signed</right> <center>title</center>")
        assert '<div style="text-align:right">signed</div>' in html
        assert '<div style="text-align:center">title</div>' in html

    def test_alignment_content_markup_and_breaks(self) -> None:
        html = markup_to_html("<right>**sig**\nline2</right>")
        assert '<div style="text-align:right"><b>sig</b><br/>line2</div>' in html

    def test_already_html_passes_through(self) -> None:
        html = "<p>already <b>html</b></p>"
        assert markup_to_html(html) == html

    def test_empty_input(self) -> None:
        assert markup_to_html("") == ""


class TestHtmlToText:
    def test_strips_tags_keeps_structure(self) -> None:
        text = html_to_text(
            '<p>Dear A,</p>\n<p>Body.</p>\n<div style="text-align:right">Sig<br/>2026年8月16日</div>'
        )
        assert "Dear A," in text
        assert "Sig" in text
        assert "2026年8月16日" in text
        assert "<" not in text

    def test_unescapes_entities(self) -> None:
        assert html_to_text("<p>a &lt; b</p>") == "a < b"


class TestBuildLetter:
    def test_chinese_letter_structure(self) -> None:
        html = build_letter(
            "cn",
            recipient="张先生",
            today=date(2026, 8, 16),
            opening="尊敬的张先生：",
            body="感谢来信。",
            signature="李四",
        )
        assert "<p>尊敬的张先生：</p>" in html
        assert "<p>感谢来信。</p>" in html
        assert "此致<br/>敬礼！" in html
        assert '<div style="text-align:right">署名：李四<br/>2026年8月16日</div>' in html

    def test_english_letter_structure(self) -> None:
        html = build_letter(
            "en",
            recipient="John",
            today=date(2026, 8, 16),
            opening="Dear John,",
            body="Thank you.",
            signature="Jane",
        )
        assert "<p>Dear John,</p>" in html
        assert "<p>Thank you.</p>" in html
        assert "<p>Sincerely,</p>" in html
        assert '<div style="text-align:right">Jane<br/>August 16, 2026</div>' in html

    def test_skeleton_defaults(self) -> None:
        cn = build_letter("cn", recipient="张先生", today=date(2026, 8, 16))
        assert "尊敬的张先生：" in cn
        assert "（正文）" in cn
        assert "署名：" in cn
        assert "2026年8月16日" in cn
        en = build_letter("en", recipient="John", today=date(2026, 8, 16))
        assert "Dear John," in en
        assert "(body)" in en

    def test_body_markup_and_roundtrip(self) -> None:
        html = build_letter(
            "cn",
            recipient="张先生",
            today=date(2026, 8, 16),
            body="**重要**内容\n\n第二段 <right>备注</right>",
            signature="李四",
        )
        assert "<b>重要</b>" in html
        assert '<div style="text-align:right">备注</div>' in html
        plain = html_to_text(html)
        assert "重要" in plain
        assert "<" not in plain

    def test_unknown_language_rejected(self) -> None:
        with pytest.raises(ValueError):
            build_letter("ja", recipient="X", today=date(2026, 8, 16))
