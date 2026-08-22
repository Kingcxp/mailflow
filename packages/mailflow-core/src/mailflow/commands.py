"""Shared management command router.

One router serves the CLI shell, chat platforms and any other host: parsing
with ``shlex``, structured colored responses, and every management operation
delegated to the service facade. Output is transport-neutral — Rich styling
travels as style *metadata*, never embedded ANSI bytes.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import shlex
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from mailflow import __version__
from mailflow.domain import (
    ActionItem,
    CommandResponse,
    ComponentKind,
    MailRecord,
    ReplyDraft,
    StyleSpan,
    Urgency,
    parse_urgency,
)
from mailflow.letters import LETTER_LANGUAGES, html_to_text, markup_to_html
from mailflow.updates import UpdateReport

if TYPE_CHECKING:
    from rich.text import Text

    from mailflow.service import MailFlowService

logger = logging.getLogger("mailflow.commands")

_STYLE_TITLE = "bold cyan"
_STYLE_HEADER = "bold"
_STYLE_OK = "green"
_STYLE_ERROR = "red"
_STYLE_MUTED = "dim"
_STYLE_USAGE = "cyan"
_STYLE_ACCENT = "magenta"

_TOPICS = (
    "mail",
    "action",
    "plugin",
    "adapter",
    "account",
    "llm",
    "reply",
    "lang",
    "trash",
    "runtime",
    "config",
    "feedback",
    "update",
)

_URGENCY_LEVELS = frozenset(u.value for u in Urgency)

_PAGE_SIZE = 10
"""Rows per page for chat-friendly listings (chat messages are length-limited)."""


def _split_flags(args: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split ``--flag value`` pairs from positional arguments.

    A value is only consumed when it does not look like another flag, so
    ``--notes --due X`` cannot silently swallow ``--due``; the bare flag
    stays a positional and the command reports its usage instead.
    """
    positionals: list[str] = []
    flags: dict[str, str] = {}
    index = 0
    while index < len(args):
        arg = args[index]
        if arg.startswith("--") and index + 1 < len(args) and not args[index + 1].startswith("--"):
            flags[arg[2:]] = args[index + 1]
            index += 2
            continue
        positionals.append(arg)
        index += 1
    return positionals, flags


def _page_number(flags: dict[str, str]) -> int:
    raw = flags.get("page", "1")
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def _paginate(items: list[Any], page: int) -> tuple[list[Any], int]:
    """Return (page slice, total pages) for 1-based ``page``."""
    total = len(items)
    pages = max(1, -(-total // _PAGE_SIZE))
    page = min(max(1, page), pages)
    start = (page - 1) * _PAGE_SIZE
    return items[start : start + _PAGE_SIZE], pages


_SPAN_OPEN = re.compile(r"<span\b[^>]*?\bstyle\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))[^>]*>")
_SPAN_CLOSE = re.compile(r"</span\s*>")
# Private-use markers smuggled through the markdown renderer, then resolved
# into span styles so `<span style="color:...">` colors text in every frontend.
_SPAN_BEGIN = "\ue000"
_SPAN_END = "\ue001"
_SPAN_STOP = "\ue002"


def _span_color(style_text: str) -> str | None:
    match = re.search(r"color\s*:\s*([^;]+)", style_text or "")
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _inject_span_markers(markdown: str) -> str:
    """Replace <span style="color:X">…</span> with sentinel markers that the
    markdown renderer treats as literal text, so the color survives parsing."""

    def open_repl(match: re.Match[str]) -> str:
        color = _span_color(match.group(1) or match.group(2) or match.group(3) or "")
        return f"{_SPAN_BEGIN}{color}{_SPAN_END}" if color else ""

    return _SPAN_CLOSE.sub(_SPAN_STOP, _SPAN_OPEN.sub(open_repl, markdown))


def _resolve_span_markers(text: Text) -> None:
    """Turn sentinel markers into real color styles on the rich Text,
    keeping every other span's offsets valid."""
    opens: list[tuple[int, str]] = []
    closes: list[int] = []
    position = 0
    while True:
        next_open = text.plain.find(_SPAN_BEGIN, position)
        next_close = text.plain.find(_SPAN_STOP, position)
        if next_open == -1 and next_close == -1:
            break
        if next_close != -1 and (next_open == -1 or next_close < next_open):
            closes.append(next_close)
            position = next_close + 1
        else:
            color_end = text.plain.index(_SPAN_END, next_open)
            color = text.plain[next_open + 1 : color_end]
            opens.append((next_open, color))
            position = color_end + 1
    for start, color in opens:
        content_start = start + 1 + len(color) + 1
        content_end = next((c for c in closes if c > content_start), len(text.plain))
        text.stylize(color, content_start, content_end)
    removals = sorted(
        [(start, 1 + len(color) + 1) for start, color in opens] + [(c, 1) for c in closes],
        key=lambda item: item[0],
        reverse=True,
    )
    for at, length in removals:
        text.spans = [
            span._replace(  # pyright: ignore[reportPrivateUsage]
                start=span.start - length if span.start >= at else span.start,
                end=span.end - length if span.end > at else span.end,
            )
            for span in text.spans
        ]
        # assign last: Text.plain's setter trims out-of-range spans, so the
        # spans must already be shifted into the new coordinate space
        text.plain = text.plain[:at] + text.plain[at + length :]


def _markdown_spans(markdown: str) -> list[StyleSpan]:
    """Render markdown as transport-neutral spans via rich, keeping headings,
    bold/italic/strike, code blocks and `<span style="color:…">` colors."""
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.text import Text

    console = Console(record=True, force_terminal=False, highlight=False, file=io.StringIO())
    console.print(Markdown(_inject_span_markers(markdown or "")))
    merged = Text()
    for text, style, _meta in console._record_buffer:  # pyright: ignore[reportPrivateUsage]
        if text:
            merged.append(text, style=style)
    _resolve_span_markers(merged)
    spans: list[StyleSpan] = []
    offset = 0
    # split("\n") keeps the offset accounting exact (splitlines also breaks
    # on \v \f \u2028 and counts \r\n as one separator)
    for line in merged.plain.split("\n"):
        line_len = len(line)
        if line_len:
            char_styles = [""] * line_len
            for span in merged.spans:
                start = max(span.start - offset, 0)
                end = min(span.end - offset, line_len)
                style_str = str(span.style) if span.style else ""
                if not style_str:
                    continue
                for index in range(start, end):
                    char_styles[index] = " ".join(
                        part for part in (char_styles[index], style_str) if part
                    )
            run_start = 0
            for index in range(1, line_len + 1):
                current = char_styles[index - 1]
                nxt = char_styles[index] if index < line_len else None
                if current != nxt:
                    spans.append(
                        StyleSpan(
                            text=line[run_start:index] + ("\n" if index == line_len else ""),
                            style=current,
                        )
                    )
                    run_start = index
        else:
            spans.append(StyleSpan(text="\n"))
        offset += line_len + 1
    return spans or [StyleSpan(text="\n")]


class CommandRouter:
    """Parses command lines and returns structured responses."""

    def __init__(self, service: MailFlowService) -> None:
        self.service = service
        service.commands = self
        self._handlers: dict[str, Callable[[list[str]], Awaitable[CommandResponse]]] = {
            "help": self._cmd_help,
            "mail": self._cmd_mail,
            "action": self._cmd_action,
            "plugin": self._cmd_plugin,
            "adapter": self._cmd_adapter,
            "account": self._cmd_account,
            "llm": self._cmd_llm,
            "reply": self._cmd_reply,
            "lang": self._cmd_lang,
            "trash": self._cmd_trash,
            "runtime": self._cmd_runtime,
            "config": self._cmd_config,
            "feedback": self._cmd_feedback,
            "update": self._cmd_update,
        }

    # -- helpers ----------------------------------------------------------------------

    def _t(self, key: str, **params: Any) -> str:
        return self.service.t(key, **params)

    def _ok(self, text: str) -> CommandResponse:
        return CommandResponse.rich([(text, _STYLE_OK)], ok=True)

    def _err(self, text: str) -> CommandResponse:
        return CommandResponse.rich([(text, _STYLE_ERROR)], ok=False)

    def _localize(self, value: datetime) -> datetime:
        return value.astimezone(ZoneInfo(self.service.config.general.timezone))

    def _fmt_time(self, value: datetime) -> str:
        return self._localize(value).strftime("%Y-%m-%d %H:%M")

    def _urgency_span(self, urgency: Urgency) -> list[StyleSpan]:
        return [
            StyleSpan(text="■ ", style=urgency.color),
            StyleSpan(text=urgency.value, style=urgency.color),
        ]

    # -- entry point --------------------------------------------------------------------

    async def execute(self, line: str) -> CommandResponse:
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            return self._err(f"parse error: {exc}")
        if not parts:
            return CommandResponse.plain("")
        command, *args = parts
        handler = self._handlers.get(command)
        if handler is None:
            return CommandResponse.rich(
                [
                    (self._t("command.unknown", command=command), _STYLE_ERROR),
                    (" — ", _STYLE_MUTED),
                    (self._t("command.help.topic_hint"), _STYLE_MUTED),
                ],
                ok=False,
            )
        try:
            return await handler(args)
        except KeyError as exc:
            return self._err(str(exc))
        except ValueError as exc:
            return self._err(str(exc))
        except PermissionError as exc:
            return self._err(str(exc))
        except Exception as exc:
            logger.error("command %r failed: %s", command, exc)
            return self._err(self._t("common.error", message=str(exc)))

    # -- help -------------------------------------------------------------------------------

    async def _cmd_help(self, args: list[str]) -> CommandResponse:
        positionals, flags = _split_flags(args)
        if not positionals:
            page = _page_number(flags)
            topics, pages = _paginate(list(_TOPICS), page)
            spans: list[StyleSpan] = [
                StyleSpan(text=self._t("command.help.title"), style=_STYLE_TITLE),
                StyleSpan(text="\n" + self._t("command.help.intro"), style=_STYLE_MUTED),
                StyleSpan(
                    text=f"\n\n{self._t('command.help.available')} — {page}/{pages}\n",
                    style=_STYLE_HEADER,
                ),
            ]
            for topic in topics:
                spans.append(StyleSpan(text=f"  {topic:<10}", style=_STYLE_USAGE))
                spans.append(StyleSpan(text=f"{self._topic_line(topic)}\n", style=_STYLE_MUTED))
            return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
        topic = positionals[0]
        if topic not in self._handlers:
            return self._err(self._t("command.help.topic_missing", topic=topic))
        return self._topic_help(topic)

    def _topic_line(self, topic: str) -> str:
        key = f"{topic}.usage" if topic != "help" else "command.help.topic_hint"
        return self._t(key) if topic in _TOPICS else self._t("command.help.topic_hint")

    def _topic_help(self, topic: str) -> CommandResponse:
        usage = self._t(f"{topic}.usage")
        spans = [
            StyleSpan(text=self._t("command.help.topic_title", topic=topic), style=_STYLE_TITLE),
            StyleSpan(text=f"\n{self._t('command.usage', usage=usage)}", style=_STYLE_USAGE),
        ]
        if topic == "mail":
            spans.append(StyleSpan(text=f"\n{self._t('mail.urgency_help')}", style=_STYLE_MUTED))
        if topic == "reply":
            spans.append(StyleSpan(text=f"\n{self._t('reply.usage')}", style=_STYLE_MUTED))
        return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))

    # -- mail ---------------------------------------------------------------------------------

    async def _cmd_mail(self, args: list[str]) -> CommandResponse:
        if not args or args[0] == "list":
            _positionals, flags = _split_flags(args[1:])
            query = flags.get("query", "").strip().lower()
            return await self._mail_list(page=_page_number(flags), query=query)
        sub, rest = args[0], args[1:]
        if sub == "show":
            return await self._mail_show(rest)
        if sub == "delete":
            return await self._mail_delete(rest)
        if sub == "urgency":
            return await self._mail_urgency(rest)
        return self._err(self._t("mail.usage"))

    async def _mail_list(self, *, page: int = 1, query: str = "") -> CommandResponse:
        records = await self.service.list_mails()
        if query:
            records = [
                record
                for record in records
                if query
                in (f"{record.mail.subject} {record.mail.sender.address} {record.summary}").lower()
            ]
        page_records, pages = _paginate(records, page)
        spans: list[StyleSpan] = [
            StyleSpan(
                text=self._t("mail.list_title", count=len(records)) + f" — {page}/{pages}",
                style=_STYLE_TITLE,
            ),
        ]
        if not records:
            spans.append(StyleSpan(text=f"\n{self._t('tui.empty')}", style=_STYLE_MUTED))
            return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
        for record in page_records:
            # compact two-line layout: wraps cleanly on narrow chat screens
            spans.append(StyleSpan(text="  "))
            spans.extend(self._urgency_span(record.effective_urgency))
            spans.append(StyleSpan(text=f" {record.mail.subject or '(no subject)'}"))
            spans.append(StyleSpan(text="\n"))
            spans.append(
                StyleSpan(
                    text=f"  {record.record_id} · {record.mail.sender.address} · "
                    f"{self._fmt_time(record.mail.received_at)}",
                    style=_STYLE_MUTED,
                )
            )
            spans.append(StyleSpan(text="\n"))
        return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))

    async def _find_mail(self, mail_id: str) -> MailRecord | None:
        """Exact id wins; otherwise a unique prefix (ids shown by the list)."""
        if not mail_id.strip():
            return None
        matches = [
            record
            for record in await self.service.list_mails()
            if record.record_id == mail_id or record.record_id.startswith(mail_id)
        ]
        return matches[0] if len(matches) == 1 else None

    async def _mail_show(self, args: list[str]) -> CommandResponse:
        if not args:
            return self._err(self._t("mail.usage"))
        record = await self._find_mail(args[0])
        if record is None:
            return self._err(self._t("mail.not_found", mail_id=args[0]))
        feedback = await self.service.get_feedback(record.record_id)
        return self._render_mail(record, feedback=feedback)

    def _render_mail(self, record: MailRecord, feedback: str | None = None) -> CommandResponse:
        mail = record.mail
        spans: list[StyleSpan] = [
            StyleSpan(
                text=self._t("mail.show_title", mail_id=record.record_id), style=_STYLE_TITLE
            ),
            StyleSpan(text=f"\n{self._t('mail.field_from')}: {mail.sender.display}"),
            StyleSpan(
                text=f"\n{self._t('mail.field_to')}: {', '.join(r.display for r in mail.recipients) or '-'}"
            ),
            StyleSpan(text=f"\n{self._t('mail.field_date')}: {self._fmt_time(mail.date)}"),
            StyleSpan(text=f"\n{self._t('mail.field_subject')}: {mail.subject}"),
            StyleSpan(text=f"\n{self._t('mail.field_account')}: {mail.account_id}"),
            StyleSpan(text=f"\n{self._t('mail.field_urgency')}: ", style=_STYLE_HEADER),
        ]
        spans.extend(self._urgency_span(record.effective_urgency))
        if record.manual_urgency is not None:
            spans.append(
                StyleSpan(
                    text=f" ({self._t('mail.field_manual')}: {record.manual_urgency.value})",
                    style=_STYLE_MUTED,
                )
            )
        else:
            spans.append(
                StyleSpan(
                    text=f" ({self._t('mail.field_auto')}: {record.auto_urgency.value})",
                    style=_STYLE_MUTED,
                )
            )
        analysis = record.analysis
        if analysis is not None:
            spans.append(StyleSpan(text=f"\n{self._t('mail.field_summary')}: {analysis.summary}"))
            if analysis.reason:
                spans.append(StyleSpan(text=f"\n{self._t('mail.field_reason')}: {analysis.reason}"))
            if analysis.reply_required:
                spans.append(
                    StyleSpan(
                        text=f"\n{self._t('mail.field_reply_required')}: ",
                        style=_STYLE_ACCENT,
                    )
                )
                spans.append(StyleSpan(text=self._t("common.yes"), style=_STYLE_ACCENT))
            if analysis.suggested_reply:
                spans.append(
                    StyleSpan(
                        text=f"\n{self._t('mail.field_suggested_reply')}: {analysis.suggested_reply}"
                    )
                )
            if analysis.action_items:
                spans.append(
                    StyleSpan(text=f"\n{self._t('mail.field_actions')}:", style=_STYLE_HEADER)
                )
                for item in analysis.action_items:
                    spans.append(
                        StyleSpan(
                            text=f"\n  {self._fmt_time(item.due_at)} {item.action_type}: {item.summary}"
                        )
                    )
            if analysis.notes:
                spans.append(StyleSpan(text=f"\n{self._t('mail.field_notes')}: {analysis.notes}"))
        if feedback:
            spans.append(
                StyleSpan(
                    text=f"\n{self._t('mail.field_feedback')}: {feedback}",
                    style=_STYLE_ACCENT,
                )
            )
        body = mail.body_text.strip() or "(no text body)"
        spans.append(StyleSpan(text=f"\n\n{self._t('mail.field_body')}:\n{body}"))
        for note in record.processor_notes:
            spans.append(
                StyleSpan(
                    text=f"\n  [{note.processor_id} {note.status}] {note.message}",
                    style=_STYLE_MUTED,
                )
            )
        return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))

    async def _mail_delete(self, args: list[str]) -> CommandResponse:
        if not args:
            return self._err(self._t("mail.usage"))
        record = await self._find_mail(args[0])
        if record is None:
            return self._err(self._t("mail.not_found", mail_id=args[0]))
        deleted = await self.service.delete_mail(record.record_id)
        if not deleted:
            return self._err(self._t("mail.not_found", mail_id=args[0]))
        return self._ok(self._t("mail.deleted", mail_id=record.record_id))

    async def _mail_urgency(self, args: list[str]) -> CommandResponse:
        if len(args) != 2:
            return self._err(self._t("mail.urgency_usage"))
        mail_id, level = args
        record = await self._find_mail(mail_id)
        if record is None:
            return self._err(self._t("mail.not_found", mail_id=mail_id))
        if level != "auto" and level not in _URGENCY_LEVELS:
            return self._err(self._t("mail.urgency_usage"))
        urgency = None if level == "auto" else parse_urgency(level)
        updated = await self.service.set_mail_urgency(record.record_id, urgency)
        if updated is None:
            return self._err(self._t("mail.not_found", mail_id=mail_id))
        if urgency is None:
            return self._ok(
                self._t(
                    "mail.urgency_reset",
                    mail_id=record.record_id,
                    urgency=updated.effective_urgency.value,
                )
            )
        return self._ok(
            self._t("mail.urgency_updated", mail_id=record.record_id, urgency=urgency.value)
        )

    # -- action items -------------------------------------------------------------------------

    async def _cmd_action(self, args: list[str]) -> CommandResponse:
        if not args or args[0] == "list":
            return await self._action_list(args[1:])
        if args[0] == "show" and len(args) == 2:
            action = await self._find_action(args[1])
            if action is None:
                return self._err(self._t("action.not_found", item_id=args[1]))
            spans = [
                StyleSpan(
                    text=self._t("action.detail_title", item_id=action.item_id),
                    style=_STYLE_TITLE,
                ),
                StyleSpan(
                    text=f"\n{self._t('action.header_time')}: {self._fmt_time(action.due_at)}"
                ),
                StyleSpan(text=f"\n{self._t('action.header_type')}: {action.action_type}"),
                StyleSpan(text=f"\n{self._t('action.header_summary')}: {action.summary}"),
                StyleSpan(text=f"\n{self._t('action.header_notes')}: {action.notes or '-'}"),
                StyleSpan(
                    text=f"\n{self._t('action.field_mail')}: "
                    f"{action.mail_id or self._t('action.source_user')}"
                ),
            ]
            return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
        if args[0] == "add":
            return await self._action_add(args[1:])
        if args[0] == "delete" and len(args) == 2:
            action = await self._find_action(args[1])
            if action is None:
                return self._err(self._t("action.not_found", item_id=args[1]))
            if action.mail_id:
                # mail-derived items are deleted with the source mail
                return self._err(self._t("action.not_found", item_id=args[1]))
            if await self.service.delete_action(action.item_id):
                return self._ok(self._t("action.deleted", item_id=action.item_id[:10]))
            return self._err(self._t("action.not_found", item_id=args[1]))
        return self._err(self._t("action.usage"))

    async def _action_list(self, args: list[str] | None = None) -> CommandResponse:
        _, flags = _split_flags(args or [])
        page = _page_number(flags)
        items = await self.service.list_actions()
        page_items, pages = _paginate(items, page)
        spans: list[StyleSpan] = [
            StyleSpan(
                text=self._t("action.title", count=len(items)) + f" — {page}/{pages}",
                style=_STYLE_TITLE,
            ),
        ]
        if not items:
            spans.append(StyleSpan(text=f"\n{self._t('action.empty')}", style=_STYLE_MUTED))
            return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
        for item in page_items:
            spans.append(
                StyleSpan(
                    text=f"  {self._fmt_time(item.due_at)} [{item.action_type}] {item.summary}"
                )
            )
            spans.append(
                StyleSpan(
                    text=f"  {item.item_id} · {(item.mail_id or self._t('action.source_user'))}"
                    + (f" · {item.notes}" if item.notes else ""),
                    style=_STYLE_MUTED,
                )
            )
            spans.append(StyleSpan(text="\n"))
        return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))

    async def _action_add(self, args: list[str]) -> CommandResponse:
        """Parse ``action add <summary> --due <time> [--type <type>] [--notes <notes>]``."""
        summary: str | None = None
        due_raw: str | None = None
        action_type = "errand"
        notes = ""
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in ("--due", "--type", "--notes"):
                if index + 1 >= len(args):
                    return self._err(self._t("action.add_usage"))
                value = args[index + 1]
                if arg == "--due":
                    due_raw = value
                elif arg == "--type":
                    action_type = value
                else:
                    notes = value
                index += 2
                continue
            summary = arg if summary is None else f"{summary} {arg}"
            index += 1
        if summary is None:
            return self._err(self._t("action.add_usage"))
        if due_raw is None:
            return self._err(self._t("action.missing_due"))
        due = self._parse_local_time(due_raw)
        if due is None:
            return self._err(self._t("action.invalid_due", value=due_raw))
        item = await self.service.add_action(summary, due, action_type=action_type, notes=notes)
        return self._ok(
            self._t(
                "action.added",
                item_id=item.item_id[:10],
                summary=item.summary,
                time=self._fmt_time(item.due_at),
            )
        )

    def _parse_local_time(self, raw: str) -> datetime | None:
        """Parse ``YYYY-MM-DD HH:MM`` in the configured timezone; naive input
        is interpreted in that zone, and the result is converted to UTC."""
        try:
            naive = datetime.strptime(raw, "%Y-%m-%d %H:%M")
        except ValueError:
            return None
        zone = ZoneInfo(self.service.config.general.timezone)
        return naive.replace(tzinfo=zone).astimezone(UTC)

    async def _find_action(self, item_id: str) -> ActionItem | None:
        """Exact id wins; otherwise a unique prefix (the list truncates ids
        to ten characters, so the shown id must resolve)."""
        if not item_id.strip():
            return None
        matches = [
            item
            for item in await self.service.list_actions()
            if item.item_id == item_id or item.item_id.startswith(item_id)
        ]
        return matches[0] if len(matches) == 1 else None

    async def _cmd_plugin_repo(self, args: list[str]) -> CommandResponse:
        if not args or args[0] == "list":
            repos = self.service.config.plugins.repositories
            spans = [
                StyleSpan(text=self._t("plugin.repo_title", count=len(repos)), style=_STYLE_TITLE),
                StyleSpan(
                    text=f"\n{self._t('plugin.header_name'):<24} {'URL'}\n",
                    style=_STYLE_HEADER,
                ),
            ]
            for repo in repos:
                spans.append(StyleSpan(text=f"{repo.name:<24} "))
                spans.append(StyleSpan(text=f"{repo.url}\n", style=_STYLE_MUTED))
            return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
        if args[0] == "add" and len(args) == 3:
            try:
                await self.service.plugin_repo_add(args[1], args[2])
            except ValueError as exc:
                return self._err(str(exc))
            return self._ok(self._t("plugin.repo_added", name=args[1]))
        if args[0] == "remove" and len(args) == 2:
            try:
                await self.service.plugin_repo_remove(args[1])
            except KeyError as exc:
                return self._err(str(exc))
            return self._ok(self._t("plugin.repo_removed", name=args[1]))
        return self._err(self._t("plugin.repo_usage"))

    async def _cmd_plugin_market(self, args: list[str]) -> CommandResponse:
        market = self.service.market
        if not args or args[0] == "list":
            category = args[1] if len(args) > 1 else ""
            entries = await asyncio.to_thread(market.list_plugins)
            if category:
                entries = [e for e in entries if category in e[1].categories]
            spans: list[StyleSpan] = [
                StyleSpan(
                    text=self._t("plugin.market_title", count=len(entries)), style=_STYLE_TITLE
                ),
                StyleSpan(
                    text=f"\n{self._t('plugin.header_id'):<34} {self._t('plugin.header_version'):<10} "
                    f"{self._t('plugin.market_categories'):<26} {self._t('plugin.market_description')}\n",
                    style=_STYLE_HEADER,
                ),
            ]
            for _repo, plugin in entries:
                installed = (
                    " [installed]" if market.is_installed(plugin.id, package=plugin.package) else ""
                )
                categories = ",".join(plugin.categories)
                description = plugin.description_for(self.service.i18n.language)
                spans.append(StyleSpan(text=f"{plugin.id:<34} "))
                spans.append(StyleSpan(text=f"{plugin.version:<10} ", style=_STYLE_MUTED))
                spans.append(StyleSpan(text=f"{categories:<26} ", style=_STYLE_ACCENT))
                spans.append(StyleSpan(text=f"{description[:40]}{installed}\n"))
            if not entries:
                spans.append(
                    StyleSpan(text=f"\n{self._t('plugin.market_empty')}", style=_STYLE_MUTED)
                )
            return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
        if args[0] == "show" and len(args) == 2:
            found = await asyncio.to_thread(market.find, args[1])
            if found is None:
                return self._err(self._t("plugin.market_not_found", plugin_id=args[1]))
            repo, plugin = found
            status = (
                self._t("plugin.installed")
                if market.is_installed(plugin.id, package=plugin.package)
                else self._t("plugin.not_installed")
            )
            language = self.service.i18n.language
            spans = [
                StyleSpan(text=f"{plugin.name or plugin.id} {plugin.version}", style=_STYLE_TITLE),
                StyleSpan(text=f"\n{self._t('plugin.header_id')}: {plugin.id}"),
                StyleSpan(
                    text=f"\n{self._t('plugin.market_categories')}: {', '.join(plugin.categories) or '-'}"
                ),
                StyleSpan(text=f"\n{self._t('plugin.market_author')}: {plugin.author or '-'}"),
                StyleSpan(text=f"\n{self._t('plugin.market_updated')}: {plugin.updated or '-'}"),
                StyleSpan(text=f"\n{self._t('plugin.market_license')}: {plugin.license or '-'}"),
                StyleSpan(text=f"\n{self._t('plugin.market_source')}: {plugin.source or '-'}"),
                StyleSpan(text=f"\n{self._t('plugin.market_repo')}: {repo.name}"),
                StyleSpan(text=f"\n{self._t('plugin.market_status')}: {status}"),
                StyleSpan(
                    text=f"\n{self._t('plugin.market_description')}: "
                    f"{plugin.description_for(language) or '-'}"
                ),
            ]
            spans.extend(
                _markdown_spans(
                    plugin.readme_for(language) or plugin.description_for(language) or "-"
                )
            )
            return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
        return self._err(self._t("plugin.market_usage"))

    async def _cmd_plugin_search(self, args: list[str]) -> CommandResponse:
        if not args:
            return self._err(self._t("plugin.search_usage"))
        query = args[0]
        category = args[1] if len(args) > 1 else ""
        entries = await asyncio.to_thread(
            self.service.market.search, query, category, self.service.i18n.language
        )
        spans: list[StyleSpan] = [
            StyleSpan(
                text=self._t("plugin.market_title", count=len(entries)) + f" — {query!r}",
                style=_STYLE_TITLE,
            ),
            StyleSpan(
                text=f"\n{'PLUGIN':<34} {'VERSION':<10} {'CATEGORIES':<26} {'DESCRIPTION'}\n",
                style=_STYLE_HEADER,
            ),
        ]
        for _repo, plugin in entries:
            installed = (
                " [installed]"
                if self.service.market.is_installed(plugin.id, package=plugin.package)
                else ""
            )
            description = plugin.description_for(self.service.i18n.language)
            spans.append(StyleSpan(text=f"{plugin.id:<34} "))
            spans.append(StyleSpan(text=f"{plugin.version:<10} ", style=_STYLE_MUTED))
            spans.append(StyleSpan(text=f"{','.join(plugin.categories):<26} ", style=_STYLE_ACCENT))
            spans.append(StyleSpan(text=f"{description[:40]}{installed}\n"))
        if not entries:
            spans.append(StyleSpan(text=f"\n{self._t('plugin.market_empty')}", style=_STYLE_MUTED))
        return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))

    async def _cmd_plugin_uninstall(self, args: list[str]) -> CommandResponse:
        if len(args) != 1:
            return self._err(self._t("plugin.uninstall_usage"))
        try:
            output = await self.service.plugin_uninstall(args[0])
        except (KeyError, ValueError, RuntimeError) as exc:
            return self._err(str(exc))
        await self.service.clear_plugin_source(args[0])
        return self._ok(
            self._t("plugin.uninstalled_ok", plugin_id=args[0]) + (f"\n{output}" if output else "")
        )

    async def _cmd_plugin_enable(self, args: list[str]) -> CommandResponse:
        if len(args) != 1:
            return self._err(self._t("plugin.enable_usage"))
        try:
            created = await self.service.plugin_enable(args[0])
        except (KeyError, ValueError) as exc:
            return self._err(str(exc))
        text = self._t("plugin.enabled_ok", plugin_id=args[0])
        if created:
            text += "\n" + self._t("plugin.instance_created", notifier_id=created)
        return self._ok(f"{text}\n({self._t('plugin.applies_now')})")

    async def _cmd_plugin_disable(self, args: list[str]) -> CommandResponse:
        if len(args) != 1:
            return self._err(self._t("plugin.disable_usage"))
        try:
            await self.service.plugin_disable(args[0])
        except (KeyError, ValueError) as exc:
            return self._err(str(exc))
        return self._ok(
            f"{self._t('plugin.disabled_ok', plugin_id=args[0])}\n({self._t('plugin.applies_now')})"
        )

    async def _cmd_plugin_install(self, args: list[str]) -> CommandResponse:
        if len(args) != 1:
            return self._err(self._t("plugin.install_usage"))
        import os

        local = Path(args[0])
        if await asyncio.to_thread(os.path.exists, str(local)):
            return await self._install_local(local)
        market = self.service.market
        found = await asyncio.to_thread(market.find, args[0])
        if found is None:
            return self._err(self._t("plugin.market_not_found", plugin_id=args[0]))
        _repo, plugin = found
        if market.is_installed(plugin.id, package=plugin.package):
            return self._ok(self._t("plugin.already_installed", plugin_id=plugin.id))
        try:
            output = await market.install(plugin)
        except (ValueError, RuntimeError) as exc:
            return self._err(str(exc))
        await self.service.record_plugin_source(plugin.id, plugin.source)
        return self._ok(
            self._t("plugin.installed_ok", plugin_id=plugin.id)
            + f" ({self._t('plugin.restart_note')})"
            + (f"\n{output}" if output else "")
        )

    async def _install_local(self, root: Path) -> CommandResponse:
        """Install plugins found under a local folder: the folder itself when
        it is one plugin, otherwise each of its plugin subfolders."""
        from mailflow.plugin_market import MarketPlugin, detect_plugin_folders

        folders = detect_plugin_folders(root)
        if not folders:
            return self._err(self._t("plugin.local_none_found", path=str(root)))
        installed: list[str] = []
        failed: list[str] = []
        for folder in folders:
            plugin_id = self._plugin_id_of(folder)
            try:
                await self.service.market.install(
                    MarketPlugin(
                        id=plugin_id,
                        name=folder.name,
                        version="",
                        categories=[],
                        package=plugin_id,
                        source=str(folder),
                    )
                )
            except (ValueError, RuntimeError) as exc:
                failed.append(f"{plugin_id}: {exc}")
                continue
            # local installs keep their source recorded so update checks can
            # see it is not a remote source (and skip auto updates)
            await self.service.record_plugin_source(plugin_id, str(folder))
            installed.append(plugin_id)
        if not installed:
            return self._err(self._t("plugin.local_failed", detail="; ".join(failed)))
        return self._ok(
            self._t("plugin.local_installed", count=len(installed), plugins=", ".join(installed))
            + (f"\n{self._t('plugin.restart_note')}" if not failed else "")
            + (f"\n{self._t('plugin.local_failed', detail='; '.join(failed))}" if failed else "")
        )

    @staticmethod
    def _plugin_id_of(folder: Path) -> str:
        """Plugin id: plugin.json id when present, else the folder name."""
        import json as jsonlib

        metadata_path = folder / "plugin.json"
        if metadata_path.is_file():
            try:
                payload = jsonlib.loads(metadata_path.read_text(encoding="utf-8"))
                plugin_id = payload.get("id")
                if isinstance(plugin_id, str) and plugin_id:
                    return plugin_id
            except (jsonlib.JSONDecodeError, OSError):
                pass
        return folder.name

    # -- plugins / adapters / accounts / llms -----------------------------------------------------

    async def _cmd_plugin(self, args: list[str]) -> CommandResponse:
        if args and args[0] == "repo":
            return await self._cmd_plugin_repo(args[1:])
        if args and args[0] == "market":
            return await self._cmd_plugin_market(args[1:])
        if args and args[0] == "search":
            return await self._cmd_plugin_search(args[1:])
        if args and args[0] == "install":
            return await self._cmd_plugin_install(args[1:])
        if args and args[0] == "uninstall":
            return await self._cmd_plugin_uninstall(args[1:])
        if args and args[0] == "enable":
            return await self._cmd_plugin_enable(args[1:])
        if args and args[0] == "disable":
            return await self._cmd_plugin_disable(args[1:])
        snapshot = self.service.snapshot()
        if not args or args[0] == "list":
            _, flags = _split_flags(args[1:])
            page = _page_number(flags)
            plugins, pages = _paginate(snapshot.plugins, page)
            spans = [
                StyleSpan(
                    text=self._t("plugin.title", count=len(snapshot.plugins))
                    + f" — {page}/{pages}",
                    style=_STYLE_TITLE,
                ),
            ]
            if not snapshot.plugins:
                spans.append(StyleSpan(text=f"\n{self._t('tui.empty')}", style=_STYLE_MUTED))
                return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
            for plugin in plugins:
                kinds = ",".join(k.value for k in plugin.kinds)
                spans.append(StyleSpan(text=f"  {plugin.plugin_id} ({plugin.version})"))
                spans.append(StyleSpan(text=f"\n  {plugin.name} · {kinds}", style=_STYLE_MUTED))
                spans.append(StyleSpan(text="\n"))
            return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
        if args[0] == "show" and len(args) == 2:
            plugin_info = snapshot.plugin(args[1])
            if plugin_info is None:
                return self._err(self._t("plugin.not_found", plugin_id=args[1]))
            spans = [
                StyleSpan(text=f"{plugin_info.plugin_id} ({plugin_info.name})", style=_STYLE_TITLE),
                StyleSpan(text=f"\n{self._t('plugin.header_version')}: {plugin_info.version}"),
                StyleSpan(text=f"\n{plugin_info.description or '-'}"),
                StyleSpan(
                    text=f"\n{self._t('plugin.field_components')}: {', '.join(plugin_info.components) or '-'}"
                ),
            ]
            return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
        return self._err(self._t("plugin.usage"))

    async def _cmd_adapter(self, args: list[str]) -> CommandResponse:
        components = [
            c for c in self.service.registry.snapshots() if c.kind == ComponentKind.MAIL_SOURCE
        ]
        spans = [
            StyleSpan(text=self._t("adapter.title", count=len(components)), style=_STYLE_TITLE),
            StyleSpan(
                text=f"\n{self._t('adapter.header_id'):<32} {self._t('adapter.header_plugin')}\n",
                style=_STYLE_HEADER,
            ),
        ]
        for component in components:
            spans.append(StyleSpan(text=f"{component.component_id:<32} "))
            spans.append(StyleSpan(text=f"{component.plugin_id}\n", style=_STYLE_MUTED))
        return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))

    async def _cmd_account(self, args: list[str]) -> CommandResponse:
        snapshot = self.service.snapshot()
        spans = [
            StyleSpan(
                text=self._t("account.title", count=len(snapshot.accounts)), style=_STYLE_TITLE
            ),
            StyleSpan(
                text=f"\n{self._t('account.header_id'):<24} {self._t('account.header_email'):<36} "
                f"{self._t('account.header_provider'):<24} {self._t('account.header_status')}\n",
                style=_STYLE_HEADER,
            ),
        ]
        for account in snapshot.accounts:
            status_key = f"account.status_{account.status}"
            status_text = (
                self._t(status_key)
                if status_key in self.service.i18n.keys(self.service.i18n.language)
                else account.status
            )
            spans.append(StyleSpan(text=f"{account.account_id:<24} "))
            spans.append(StyleSpan(text=f"{account.email:<36} "))
            spans.append(StyleSpan(text=f"{account.provider:<24} ", style=_STYLE_MUTED))
            color = _STYLE_ERROR if account.status == "error" else _STYLE_OK
            spans.append(StyleSpan(text=f"{status_text}\n", style=color))
        return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))

    async def _cmd_llm(self, args: list[str]) -> CommandResponse:
        snapshot = self.service.snapshot()
        if args and args[0] == "bindings":
            spans = [
                StyleSpan(text=self._t("llm.bindings_title"), style=_STYLE_TITLE),
                StyleSpan(text="\n"),
            ]
            for binding in snapshot.processors:
                if binding.llm_id:
                    fallback = ", ".join(binding.fallback_llm_ids) or "-"
                    text = self._t(
                        "llm.binding_row",
                        processor=binding.processor_id,
                        llm=binding.llm_id,
                        fallback=fallback,
                    )
                else:
                    text = self._t("llm.binding_none", processor=binding.processor_id)
                spans.append(StyleSpan(text=f"  {text}\n"))
            return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
        spans = [
            StyleSpan(text=self._t("llm.title", count=len(snapshot.llms)), style=_STYLE_TITLE),
            StyleSpan(
                text=f"\n{self._t('llm.header_id'):<20} {self._t('llm.header_name'):<20} "
                f"{self._t('llm.header_backend'):<30} {self._t('llm.header_model'):<20} {self._t('llm.header_default')}\n",
                style=_STYLE_HEADER,
            ),
        ]
        for llm in snapshot.llms:
            spans.append(StyleSpan(text=f"{llm.llm_id:<20} "))
            spans.append(StyleSpan(text=f"{llm.name:<20} "))
            spans.append(StyleSpan(text=f"{llm.backend:<30} ", style=_STYLE_MUTED))
            spans.append(StyleSpan(text=f"{llm.model:<20} "))
            spans.append(
                StyleSpan(
                    text=f"{self._t('common.yes') if llm.default else ''}\n",
                    style=_STYLE_ACCENT if llm.default else "",
                )
            )
        return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))

    # -- reply --------------------------------------------------------------------------------------

    async def _cmd_reply(self, args: list[str]) -> CommandResponse:
        if not args:
            return self._err(self._t("reply.usage"))
        sub, rest = args[0], args[1:]
        if sub == "create" and rest:
            draft = await self.service.create_reply(rest[0])
            return self._ok(self._t("reply.created", draft_id=draft.draft_id, mail_id=rest[0]))
        if sub == "compose" and len(rest) == 2:
            language = rest[1].lower()
            if language not in LETTER_LANGUAGES:
                return self._err(self._t("reply.template_unknown", language=rest[1]))
            draft = await self.service.create_letter_draft(rest[0], language)
            return self._ok(
                self._t(
                    "reply.composed",
                    draft_id=draft.draft_id,
                    mail_id=rest[0],
                    language=language,
                )
            )
        if sub == "show" and rest:
            shown = await self.service.get_draft(rest[0])
            if shown is None:
                return self._err(self._t("reply.draft_not_found", draft_id=rest[0]))
            return self._render_draft(shown)
        if sub == "edit" and len(rest) >= 3:
            body = markup_to_html(" ".join(rest[2:]))
            draft = await self.service.edit_draft(rest[0], rest[1], body)
            return CommandResponse.rich(
                [
                    (self._t("reply.edited", draft_id=draft.draft_id), _STYLE_OK),
                    (f"\n{self._t('reply.markup_help')}", _STYLE_MUTED),
                ]
            )
        if sub == "prepare" and rest:
            draft = await self.service.prepare_reply(rest[0])
            expires = self._fmt_time(draft.token_expires_at) if draft.token_expires_at else "-"
            return CommandResponse.rich(
                [
                    (
                        self._t(
                            "reply.prepared",
                            draft_id=draft.draft_id,
                            token=draft.token or "",
                            expires=expires,
                        ),
                        _STYLE_OK,
                    ),
                ]
            )
        if sub == "confirm" and len(rest) == 2:
            draft = await self.service.confirm_reply(rest[0], rest[1])
            return self._ok(self._t("reply.sent", draft_id=draft.draft_id))
        if sub == "cancel" and rest:
            draft = await self.service.cancel_reply(rest[0])
            return self._ok(self._t("reply.cancelled", draft_id=draft.draft_id))
        return self._err(self._t("reply.usage"))

    def _render_draft(self, draft: ReplyDraft) -> CommandResponse:
        spans = [
            StyleSpan(
                text=self._t("reply.show_title", draft_id=draft.draft_id), style=_STYLE_TITLE
            ),
            StyleSpan(text=f"\n{self._t('reply.field_mail')}: {draft.mail_id}"),
            StyleSpan(text=f"\n{self._t('reply.field_to')}: {draft.to.display}"),
            StyleSpan(text=f"\n{self._t('reply.field_subject')}: {draft.subject}"),
            StyleSpan(text=f"\n{self._t('reply.field_state')}: {draft.state.value}"),
            StyleSpan(
                text=f"\n{self._t('reply.field_body')}:\n{html_to_text(draft.body)}",
                style=_STYLE_MUTED,
            ),
        ]
        return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))

    # -- feedback -------------------------------------------------------------------------------------

    async def _cmd_update(self, args: list[str]) -> CommandResponse:
        """``update <check|now|status|auto <on|off>>``."""
        sub = args[0] if args else ""
        if sub == "check":
            report = await self.service.check_updates()
            return self._render_update_report(report)
        if sub == "now":
            results = await self.service.apply_updates()
            spans: list[StyleSpan] = [
                StyleSpan(text=self._t("update.applied_title"), style=_STYLE_TITLE),
            ]
            if not results:
                spans.append(
                    StyleSpan(text=f"\n{self._t('update.up_to_date')}", style=_STYLE_MUTED)
                )
            for key, outcome in results.items():
                spans.append(StyleSpan(text=f"\n  {key}: {outcome}"))
            return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
        if sub == "status":
            auto = self.service.config.general.auto_update
            return self._ok(
                self._t(
                    "update.status",
                    version=__version__,
                    auto=self._t("common.yes" if auto else "common.no"),
                )
            )
        if sub == "auto" and len(args) == 2 and args[1] in ("on", "off"):
            enabled = args[1] == "on"
            await self.service.set_config_value(
                "general.auto_update", "true" if enabled else "false"
            )
            return self._ok(
                self._t("update.auto_set", state=self._t("common.yes" if enabled else "common.no"))
            )
        return self._err(self._t("update.usage"))

    def _render_update_report(self, report: UpdateReport) -> CommandResponse:
        spans: list[StyleSpan] = [
            StyleSpan(text=self._t("update.title"), style=_STYLE_TITLE),
            StyleSpan(
                text=f"\n{self._t('update.mailflow', current=report.mailflow_current)}",
                style=_STYLE_HEADER,
            ),
        ]
        if report.mailflow_update:
            spans.append(
                StyleSpan(
                    text=f" → {report.mailflow_latest} ({self._t('update.available')})",
                    style=_STYLE_ACCENT,
                )
            )
        else:
            spans.append(StyleSpan(text=f" ({self._t('update.up_to_date')})", style=_STYLE_MUTED))
        if report.plugin_updates:
            for plugin_id, (old, new) in report.plugin_updates.items():
                spans.append(
                    StyleSpan(
                        text=f"\n  {plugin_id}: {old} → {new}",
                        style=_STYLE_ACCENT,
                    )
                )
        else:
            spans.append(
                StyleSpan(text=f"\n{self._t('update.plugins_up_to_date')}", style=_STYLE_MUTED)
            )
        return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))

    async def _cmd_feedback(self, args: list[str]) -> CommandResponse:
        """``feedback <mail_id> <reason...>`` — teach the LLM what to ignore."""
        if len(args) < 2:
            return self._err(self._t("feedback.usage"))
        mail_id = args[0]
        record = await self._find_mail(mail_id)
        if record is None:
            return self._err(self._t("mail.not_found", mail_id=mail_id))
        reason = " ".join(args[1:]).strip()
        try:
            await self.service.record_feedback(record.record_id, reason)
        except ValueError as exc:
            return self._err(str(exc))
        return self._ok(self._t("feedback.added", mail_id=record.record_id))

    async def _cmd_lang(self, args: list[str]) -> CommandResponse:
        if not args or args[0] == "get":
            code = await self.service.get_language()
            name = next(
                (
                    info.name
                    for info in self.service.i18n.available_languages()
                    if info.code == code
                ),
                code,
            )
            return self._ok(self._t("command.lang.current", language=code, name=name))
        if args[0] == "set" and len(args) == 2:
            try:
                await self.service.set_language(args[1])
            except KeyError:
                available = ", ".join(self.service.available_languages())
                return self._err(self._t("command.lang.unknown", language=args[1], list=available))
            name = next(
                (
                    info.name
                    for info in self.service.i18n.available_languages()
                    if info.code == args[1]
                ),
                args[1],
            )
            return self._ok(self._t("command.lang.set", language=args[1], name=name))
        return self._err(self._t("command.lang.usage"))

    # -- trash --------------------------------------------------------------------------------------------

    async def _cmd_trash(self, args: list[str]) -> CommandResponse:
        if not args or args[0] == "list":
            items = await self.service.list_trash()
            spans = [
                StyleSpan(text=self._t("mail.trash_title", count=len(items)), style=_STYLE_TITLE),
            ]
            if not items:
                spans.append(StyleSpan(text=f"\n{self._t('mail.trash_empty')}", style=_STYLE_MUTED))
                return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
            header = (
                f"{self._t('mail.header_id'):<26} {'':<2} {self._t('mail.header_urgency'):<10} "
                f"{self._t('mail.header_subject'):<40} {self._t('mail.header_deleted'):<16}"
            )
            spans.append(StyleSpan(text=f"\n{header}\n", style=_STYLE_HEADER))
            for item in items:
                spans.append(StyleSpan(text=f"{item.record_id:<26} ", style=_STYLE_MUTED))
                spans.extend(self._urgency_span(item.to_mail_record().effective_urgency))
                spans.append(StyleSpan(text=f"{item.mail.subject[:38]:<40} "))
                spans.append(StyleSpan(text=self._fmt_time(item.deleted_at), style=_STYLE_MUTED))
                spans.append(StyleSpan(text="\n"))
            return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
        if args[0] == "restore" and len(args) == 2:
            record = await self.service.restore_mail(args[1])
            if record is None:
                return self._err(self._t("mail.trash_not_found", mail_id=args[1]))
            return self._ok(self._t("mail.restored", mail_id=args[1]))
        return self._err(self._t("mail.trash_usage"))

    # -- configuration ------------------------------------------------------------------

    async def _cmd_config(self, args: list[str]) -> CommandResponse:
        if not args or args[0] == "list":
            group = args[1] if len(args) > 1 else ""
            options = self.service.list_config_options()
            if group:
                options = [o for o in options if o.group == group]
            spans: list[StyleSpan] = [
                StyleSpan(text=self._t("config.title", count=len(options)), style=_STYLE_TITLE),
                StyleSpan(
                    text=f"\n{'OPTION':<32} {'TYPE':<18} {'REQ':<5} {'VALUE':<22} {'DESCRIPTION'}\n",
                    style=_STYLE_HEADER,
                ),
            ]
            for option in options:
                key = option.key + ("*" if option.is_secret() else "")
                value = option.value
                if value is None:
                    value_text = "-"
                elif isinstance(value, bool):
                    value_text = "true" if value else "false"
                elif isinstance(value, (str, int, float)):
                    value_text = str(value)[:20]
                elif isinstance(value, (list, dict)):
                    value_text = f"{len(value)} items"  # pyright: ignore[reportUnknownArgumentType]
                else:
                    value_text = "..."  # nested model
                required = self._t("common.yes") if option.required else ""
                spans.append(StyleSpan(text=f"{key:<32} "))
                spans.append(StyleSpan(text=f"{option.type_name:<18} ", style=_STYLE_MUTED))
                spans.append(
                    StyleSpan(
                        text=f"{required:<5} ", style=_STYLE_ACCENT if option.required else ""
                    )
                )
                spans.append(StyleSpan(text=f"{value_text:<22} "))
                spans.append(StyleSpan(text=f"{option.description}\n"))
            spans.append(
                StyleSpan(
                    text=f"\n{self._t('config.legend')}",
                    style=_STYLE_MUTED,
                )
            )
            return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
        if args[0] == "get" and len(args) == 2:
            try:
                option = self.service.get_config_option(args[1])
            except KeyError as exc:
                return self._err(str(exc))
            secret_marker = " (secret)" if option.is_secret() else ""
            return self._ok(f"{option.key}{secret_marker} = {option.value}")
        if args[0] == "set" and len(args) == 3:
            try:
                option = await self.service.set_config_value(args[1], args[2])
            except (KeyError, ValueError) as exc:
                return self._err(str(exc))
            return self._ok(
                self._t(
                    "config.set_ok",
                    option=option.key,
                    value="***" if option.is_secret() else option.value,
                )
                + f" ({self._t('config.restart_note')})"
            )
        return self._err(self._t("config.usage"))

    # -- runtime --------------------------------------------------------------------------------------------

    async def _cmd_runtime(self, args: list[str]) -> CommandResponse:
        snapshot = self.service.snapshot()
        spans = [
            StyleSpan(text=self._t("runtime.title"), style=_STYLE_TITLE),
            StyleSpan(
                text=f"\n{self._t('runtime.version', version=snapshot.version)} | "
                f"{self._t('runtime.started', when=self._fmt_time(snapshot.started_at))}",
                style=_STYLE_MUTED,
            ),
            StyleSpan(
                text=f"\n{self._t('runtime.language', language=snapshot.language)} | "
                f"{self._t('runtime.timezone', timezone=snapshot.timezone)} | "
                f"{self._t('runtime.storage', provider=snapshot.storage or '-')}"
            ),
        ]
        for section, rows in (
            ("plugin", [(p.plugin_id, p.name) for p in snapshot.plugins]),
            ("account", [(a.account_id, a.email) for a in snapshot.accounts]),
            ("llm", [(llm.llm_id, llm.model) for llm in snapshot.llms]),
        ):
            if rows:
                spans.append(
                    StyleSpan(
                        text=f"\n{self._t(f'{section}.title', count=len(rows))}:",
                        style=_STYLE_HEADER,
                    )
                )
                for key, value in rows:
                    spans.append(StyleSpan(text=f"\n  {key}: {value}", style=_STYLE_MUTED))
        return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))


__all__ = ["CommandRouter"]
