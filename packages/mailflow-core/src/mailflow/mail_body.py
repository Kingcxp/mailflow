"""Readable mail bodies shared by sources, readers, search and analysis."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from mailflow.domain import MailMessage, looks_binary

_BLOCKS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "caption",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "header",
    "hr",
    "main",
    "section",
    "table",
    "tr",
}
_PARAGRAPHS = {"p", "h1", "h2", "h3", "h4", "h5", "h6"}
_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "wbr",
}
_HIDDEN = {"head", "script", "style", "template", "noscript", "iframe", "object", "svg"}
_HTML = re.compile(
    r"</?(?:html|body|p|div|br|span|a|b|i|u|strong|em|table|tr|td|th|ul|ol|li|"
    r"blockquote|pre|code|img|hr|h[1-6]|section|article|head|style|script)\b(?:\s[^<>]*|/?)>",
    re.IGNORECASE,
)


class _ReadableHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []
        self.lists: list[tuple[str, int]] = []
        self.links: list[tuple[str, int]] = []
        self.pre = 0

    def line_break(self, count: int = 1) -> None:
        if not self.parts:
            return
        last = self.parts[-1].rstrip(" \t")
        existing = len(last) - len(last.rstrip("\n"))
        self.parts[-1] = last.rstrip("\n") + "\n" * max(count, existing)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if self.hidden:
            if tag not in _VOID:
                self.hidden.append(tag)
            return
        style = attributes.get("style") or ""
        if (
            tag in _HIDDEN
            or "hidden" in attributes
            or attributes.get("aria-hidden") == "true"
            or re.search(
                r"(?:display\s*:\s*none|visibility\s*:\s*hidden|mso-hide\s*:\s*all)", style, re.I
            )
        ):
            if tag not in _VOID:
                self.hidden.append(tag)
            return
        if tag in _PARAGRAPHS:
            self.line_break(2)
        elif tag in _BLOCKS:
            self.line_break()
        elif tag == "br":
            self.line_break(2 if self.parts and self.parts[-1].endswith("\n") else 1)
        elif tag in {"pre", "textarea"}:
            self.line_break()
            self.pre += 1
        elif tag in {"ul", "ol"}:
            self.line_break()
            try:
                start = int(attributes.get("start") or "1") - 1
            except ValueError:
                start = 0
            self.lists.append((tag, start))
        elif tag == "li":
            self.line_break()
            marker = "• "
            if self.lists and self.lists[-1][0] == "ol":
                count = self.lists[-1][1] + 1
                self.lists[-1] = ("ol", count)
                marker = f"{count}. "
            self.parts.append("  " * max(0, len(self.lists) - 1) + marker)
        elif tag in {"td", "th"}:
            if self.parts and not self.parts[-1].endswith(("\n", "\t")):
                self.parts.append("\t")
        elif tag == "a":
            url = (attributes.get("href") or "").strip()
            self.links.append((url, len(self.parts)))
        elif tag == "img":
            self.handle_data(attributes.get("alt") or "")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            if tag in self.hidden:
                del self.hidden[len(self.hidden) - 1 - self.hidden[::-1].index(tag) :]
            return
        if tag in _PARAGRAPHS:
            self.line_break(2)
        elif tag in _BLOCKS or tag == "li":
            self.line_break()
        elif tag in {"ul", "ol"}:
            if self.lists:
                self.lists.pop()
            self.line_break()
        elif tag in {"pre", "textarea"}:
            self.pre = max(0, self.pre - 1)
            self.line_break()
        elif tag == "a" and self.links:
            url, start = self.links.pop()
            label = "".join(self.parts[start:]).strip()
            if (
                url.lower().startswith(("https://", "http://", "mailto:", "tel:"))
                and label != url
                and label != url.removeprefix("mailto:").removeprefix("tel:")
            ):
                self.parts.append(f" ({url})")

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        data = data.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
        if not self.pre:
            data = re.sub(r"\s+", " ", data)
            if not self.parts or self.parts[-1].endswith(("\n", " ", "\t")):
                data = data.lstrip()
        if data:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Keep visible content, paragraphs, lists, table cells and link destinations."""
    parser = _ReadableHTML()
    parser.feed(html)
    parser.close()
    return "".join(parser.parts).strip()


def mail_body_text(mail: MailMessage) -> str:
    """Handle HTML-only mail and HTML incorrectly labelled as plain text in old stores."""
    body = mail.body_text.strip()
    if looks_binary(body):
        body = ""
    if body:
        return html_to_text(body) if _HTML.search(body) else body
    return html_to_text(mail.body_html) if mail.body_html else ""
