"""Letter templates and a lightweight markup dialect for mail replies.

Two cooperating pieces:

- ``markup_to_html`` / ``html_to_text`` — a tiny markup dialect for chat
  clients and the TUI toolbar: ``**bold**``, ``*italic*``,
  ``<right>…</right>`` and ``<center>…</center>``, blank lines split
  paragraphs and single newlines become line breaks. Chat users can type
  this on a phone; the TUI toolbar inserts the same constructs. Input that
  already looks like HTML passes through untouched.
- ``build_letter`` — Chinese / English formal-letter templates: opening,
  body, closing and a right-aligned signature block with an automatic date,
  returned as an HTML fragment (standard for mail clients).

The reply body is stored as this HTML fragment; ``html_to_text`` renders a
plain-text view for terminals and chats.
"""

from __future__ import annotations

import html as _html
import re
from datetime import date

_ALREADY_HTML = re.compile(
    r"</?(?:p|div|br|b|i|u|strong|em|table|ul|ol|li|blockquote)\b", re.IGNORECASE
)
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"\*([^*\n]+)\*")
_ALIGN = re.compile(r"<(right|center)>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")

_EN_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

LETTER_LANGUAGES = ("cn", "en")


def _format_date(when: date, language: str) -> str:
    if language == "cn":
        return f"{when.year}年{when.month}月{when.day}日"
    return f"{_EN_MONTHS[when.month - 1]} {when.day}, {when.year}"


def _inline_markup(text: str) -> str:
    """Escape first, then apply **bold** / *italic* and newline breaks, so the
    generated tags survive escaping."""
    text = _html.escape(text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    return text.replace("\n", "<br/>")


def _align_block(match: re.Match[str]) -> str:
    align = match.group(1).lower()
    content = _inline_markup(match.group(2))
    return f'<div style="text-align:{align}">{content}</div>'


def _paragraph(markup: str) -> str:
    """One paragraph: alignment blocks are extracted first, then the rest is
    escaped and inline-marked."""
    parts: list[str] = []
    position = 0
    for match in _ALIGN.finditer(markup):
        parts.append(_inline_markup(markup[position : match.start()]))
        parts.append(_align_block(match))
        position = match.end()
    parts.append(_inline_markup(markup[position:]))
    return "<p>" + "".join(parts) + "</p>"


def markup_to_html(text: str) -> str:
    """Convert the lightweight markup dialect to an HTML fragment.

    Blank lines split paragraphs; single newlines become line breaks.
    ``**bold**``, ``*italic*``, ``<right>…</right>`` and
    ``<center>…</center>`` are honoured inside paragraphs. Input that
    already contains HTML block tags is returned unchanged.
    """
    if _ALREADY_HTML.search(text):
        return text
    paragraphs = re.split(r"\n\s*\n", text.strip("\n"))
    return "\n".join(_paragraph(p) for p in paragraphs if p.strip())


def html_to_text(html_text: str) -> str:
    """Strip an HTML fragment to plain text (breaks and paragraphs kept)."""
    text = html_text.replace("<br/>", "\n").replace("<br>", "\n")
    text = re.sub(r"</(p|div)>", "\n\n", text, flags=re.IGNORECASE)
    text = _TAG.sub("", text)
    return _html.unescape(text).strip("\n")


def build_letter(
    language: str,
    *,
    recipient: str,
    today: date,
    opening: str = "",
    body: str = "",
    signature: str = "",
) -> str:
    """Compose a formal letter as an HTML fragment.

    ``language`` is ``"cn"`` or ``"en"``. The date is filled automatically
    and the signature block is right-aligned (Chinese letter convention).
    Empty ``opening``/``body``/``signature`` produce a skeleton with
    sensible defaults and placeholders for the user to fill in.
    """
    if language not in LETTER_LANGUAGES:
        raise ValueError(f"unknown letter language {language!r}; use {LETTER_LANGUAGES}")
    if language == "cn":
        opening_line = opening or f"尊敬的{recipient}："
        closing = "此致<br/>敬礼！"
        body_html = markup_to_html(body) if body.strip() else _paragraph("（正文）")
        signature_line = f"署名：{signature}" if signature else "署名："
    else:
        opening_line = opening or f"Dear {recipient},"
        closing = "Sincerely,"
        body_html = markup_to_html(body) if body.strip() else _paragraph("(body)")
        signature_line = signature
    closing_div = f'<div style="text-align:right">{_inline_markup(signature_line)}<br/>{_format_date(today, language)}</div>'
    return "\n".join(
        [
            _paragraph(opening_line),
            body_html,
            f"<p>{closing}</p>",
            closing_div,
        ]
    )


__all__ = ["LETTER_LANGUAGES", "build_letter", "html_to_text", "markup_to_html"]
