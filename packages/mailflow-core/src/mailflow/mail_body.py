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
_HTML_TAGS = (
    _BLOCKS
    | _PARAGRAPHS
    | _VOID
    | _HIDDEN
    | {
        "html",
        "body",
        "span",
        "a",
        "b",
        "i",
        "u",
        "strong",
        "em",
        "s",
        "del",
        "ol",
        "ul",
        "li",
        "td",
        "th",
        "thead",
        "tbody",
        "tfoot",
        "pre",
        "code",
        "textarea",
        "center",
        "right",
    }
)
_HTML = re.compile(
    r"</?(?:" + "|".join(sorted(_HTML_TAGS)) + r")\b(?:\s[^<>]*|/?)>",
    re.IGNORECASE,
)
_MARKDOWN_ESCAPE = re.compile(r"([\\`*_{}\[\]<>#!|~.+-])")
_MARKDOWN_CODE = re.compile(
    r"^ {0,3}(?P<fence>`{3,}|~{3,})[^\n]*(?:\n|$).*?"
    r"(?:^ {0,3}(?P=fence)[`~]*[ \t]*(?:\n|$)|\Z)"
    r"|(?<!`)(?P<ticks>`+)(?!`).*?(?<!`)(?P=ticks)(?!`)",
    re.MULTILINE | re.DOTALL,
)
_EMPHASIS = {"b": "**", "strong": "**", "i": "*", "em": "*", "s": "~~", "del": "~~"}


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


class _MarkdownHTML(_ReadableHTML):
    """Reuse mail HTML visibility rules, preserving readable emphasis and code."""

    def __init__(self, *, mixed: bool = False) -> None:
        super().__init__()
        self.mixed = mixed
        self.code: list[tuple[str, int]] = []
        self.emphasis: list[tuple[str, int]] = []
        self.quotes: list[int] = []

    def line_break(self, count: int = 1) -> None:
        super().line_break(count)
        if (
            count == 1
            and self.parts
            and self.parts[-1].endswith("\n")
            and not self.parts[-1].endswith("\n\n")
        ):
            self.parts[-1] = self.parts[-1].rstrip() + "  \n"

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        if self.pre or self.code:
            self.parts.append(data.replace("\r\n", "\n").replace("\r", "\n"))
        elif self.mixed:
            self.parts.append(data.replace("<", r"\<").replace(">", r"\>").replace("\xa0", " "))
        else:
            super().handle_data(_MARKDOWN_ESCAPE.sub(r"\\\1", data))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.mixed and not self.hidden and tag not in _HTML_TAGS:
            raw_tag = self.get_starttag_text() or f"<{tag}>"
            self.parts.append(_MARKDOWN_ESCAPE.sub(r"\\\1", raw_tag))
            return
        super().handle_starttag(tag, attrs)
        if self.hidden:
            return
        if tag in {"pre", "code"} and (tag == "pre" or not self.pre):
            self.code.append((tag, len(self.parts)))
        elif not self.pre and not self.code:
            if tag in _EMPHASIS:
                self.emphasis.append((tag, len(self.parts)))
            elif tag in _PARAGRAPHS - {"p"}:
                self.parts.append("#" * int(tag[1]) + " ")
            elif tag == "li" and self.parts[-1].endswith("• "):
                self.parts[-1] = self.parts[-1][:-2] + "- "
            elif tag in {"td", "th"} and self.parts and self.parts[-1] == "\t":
                self.parts[-1] = " | "
            elif tag == "blockquote":
                self.quotes.append(len(self.parts))

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            super().handle_endtag(tag)
            return
        if self.mixed and tag not in _HTML_TAGS:
            self.parts.append(_MARKDOWN_ESCAPE.sub(r"\\\1", f"</{tag}>"))
            return
        if self.code and tag == self.code[-1][0]:
            _, start = self.code.pop()
            content = "".join(self.parts[start:])
            longest = max((len(m[0]) for m in re.finditer(r"`+", content)), default=0)
            fence = "`" * max(3 if tag == "pre" else 1, longest + 1)
            if tag == "pre":
                content = content.rstrip("\n")
                self.parts[start:] = [f"\n{fence}\n{content}\n{fence}\n"]
            else:
                padding = " " if content.startswith("`") or content.endswith("`") else ""
                self.parts[start:] = [f"{fence}{padding}{content}{padding}{fence}"]
        elif not self.pre and not self.code:
            if self.emphasis and tag == self.emphasis[-1][0]:
                _, start = self.emphasis.pop()
                content = "".join(self.parts[start:])
                marker = _EMPHASIS[tag]
                stripped = content.strip()
                if stripped:
                    self.parts[start:] = [content.replace(stripped, marker + stripped + marker, 1)]
            elif tag == "a" and self.links:
                url, start = self.links.pop()
                label = "".join(self.parts[start:]).strip()
                if url.lower().startswith(("https://", "http://", "mailto:", "tel:")):
                    url = url.replace("(", "%28").replace(")", "%29").replace(" ", "%20")
                    self.parts[start:] = [f"[{label or url}](<{url}>)"]
                return
            elif tag == "blockquote" and self.quotes:
                start = self.quotes.pop()
                content = "".join(self.parts[start:]).strip()
                self.parts[start:] = ["\n".join("> " + line for line in content.splitlines())]
        super().handle_endtag(tag)


def html_to_markdown(html: str) -> str:
    """Format visible mail HTML without executing styles or fetching remote content."""
    parser = _MarkdownHTML()
    parser.feed(html)
    parser.close()
    return "".join(parser.parts).strip()


def readable_markdown(text: str, *, preserve_lines: bool = False) -> str:
    """Normalize HTML mixed into Markdown, keeping code examples literal."""
    if _HTML.search(text):
        parser = _MarkdownHTML(mixed=True)
        position = 0
        for match in _MARKDOWN_CODE.finditer(text):
            parser.feed(text[position : match.start()])
            if not parser.hidden:
                parser.parts.append(match[0])
            position = match.end()
        parser.feed(text[position:])
        parser.close()
        text = "".join(parser.parts).strip()
    if preserve_lines:
        parts: list[str] = []
        position = 0
        for match in _MARKDOWN_CODE.finditer(text):
            parts.extend((re.sub(r"(?<!\\)\n", "  \n", text[position : match.start()]), match[0]))
            position = match.end()
        parts.append(re.sub(r"(?<!\\)\n", "  \n", text[position:]))
        text = "".join(parts)
    return text


def mail_body_markdown(mail: MailMessage) -> str:
    """Use the original HTML formatting, falling back to the stored text body."""
    if mail.body_html:
        formatted = html_to_markdown(mail.body_html)
        if formatted:
            return formatted
    body = mail.body_text.strip()
    return readable_markdown(body) if not looks_binary(body) else ""


def mail_body_text(mail: MailMessage) -> str:
    """Handle HTML-only mail and HTML incorrectly labelled as plain text in old stores."""
    body = mail.body_text.strip()
    if looks_binary(body):
        body = ""
    if body:
        return html_to_text(body) if _HTML.search(body) else body
    return html_to_text(mail.body_html) if mail.body_html else ""
