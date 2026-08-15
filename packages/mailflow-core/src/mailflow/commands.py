"""Shared management command router.

One router serves the CLI shell, chat platforms and any other host: parsing
with ``shlex``, structured colored responses, and every management operation
delegated to the service facade. Output is transport-neutral — Rich styling
travels as style *metadata*, never embedded ANSI bytes.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

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

if TYPE_CHECKING:
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
)


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
        if not args:
            spans: list[StyleSpan] = [
                StyleSpan(text=self._t("command.help.title"), style=_STYLE_TITLE),
                StyleSpan(text="\n" + self._t("command.help.intro"), style=_STYLE_MUTED),
                StyleSpan(text=f"\n\n{self._t('command.help.available')}\n", style=_STYLE_HEADER),
            ]
            for topic in _TOPICS:
                spans.append(StyleSpan(text=f"  {topic:<10}", style=_STYLE_USAGE))
                spans.append(StyleSpan(text=f"{self._topic_line(topic)}\n", style=_STYLE_MUTED))
            return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
        topic = args[0]
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
            limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 50
            return await self._mail_list(limit)
        sub, rest = args[0], args[1:]
        if sub == "show":
            return await self._mail_show(rest)
        if sub == "delete":
            return await self._mail_delete(rest)
        if sub == "urgency":
            return await self._mail_urgency(rest)
        return self._err(self._t("mail.usage"))

    async def _mail_list(self, limit: int) -> CommandResponse:
        records = await self.service.list_mails(limit=limit)
        spans: list[StyleSpan] = [
            StyleSpan(text=self._t("mail.list_title", count=len(records)), style=_STYLE_TITLE),
        ]
        if not records:
            spans.append(StyleSpan(text=f"\n{self._t('tui.empty')}", style=_STYLE_MUTED))
            return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
        header = f"{'ID':<26} {'':<2} {'URGENCY':<10} {'SUBJECT':<40} {'FROM':<28} {'DATE':<16}"
        spans.append(StyleSpan(text=f"\n{header}\n", style=_STYLE_HEADER))
        for record in records:
            spans.append(StyleSpan(text=f"{record.record_id:<26} ", style=_STYLE_MUTED))
            spans.extend(self._urgency_span(record.effective_urgency))
            subject = record.mail.subject or "(no subject)"
            spans.append(StyleSpan(text=f"{subject[:38]:<40} "))
            spans.append(StyleSpan(text=f"{record.mail.sender.address[:26]:<28} "))
            spans.append(
                StyleSpan(
                    text=self._fmt_time(record.mail.received_at),
                    style=_STYLE_MUTED,
                )
            )
            spans.append(StyleSpan(text="\n"))
        return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))

    async def _mail_show(self, args: list[str]) -> CommandResponse:
        if not args:
            return self._err(self._t("mail.usage"))
        record = await self.service.get_mail(args[0])
        if record is None:
            return self._err(self._t("mail.not_found", mail_id=args[0]))
        return self._render_mail(record)

    def _render_mail(self, record: MailRecord) -> CommandResponse:
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
        deleted = await self.service.delete_mail(args[0])
        if not deleted:
            return self._err(self._t("mail.not_found", mail_id=args[0]))
        return self._ok(self._t("mail.deleted", mail_id=args[0]))

    async def _mail_urgency(self, args: list[str]) -> CommandResponse:
        if len(args) != 2:
            return self._err(self._t("mail.urgency_usage"))
        mail_id, level = args
        urgency = None if level == "auto" else parse_urgency(level)
        record = await self.service.set_mail_urgency(mail_id, urgency)
        if record is None:
            return self._err(self._t("mail.not_found", mail_id=mail_id))
        if urgency is None:
            return self._ok(
                self._t(
                    "mail.urgency_reset", mail_id=mail_id, urgency=record.effective_urgency.value
                )
            )
        return self._ok(self._t("mail.urgency_updated", mail_id=mail_id, urgency=urgency.value))

    # -- action items -------------------------------------------------------------------------

    async def _cmd_action(self, args: list[str]) -> CommandResponse:
        if not args or args[0] == "list":
            items = await self.service.list_actions()
            spans: list[StyleSpan] = [
                StyleSpan(text=self._t("action.title", count=len(items)), style=_STYLE_TITLE),
            ]
            if not items:
                spans.append(StyleSpan(text=f"\n{self._t('action.empty')}", style=_STYLE_MUTED))
                return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
            header = f"{'ID':<12} {'TIME':<18} {'TYPE':<10} {'CONTENT':<32} {'NOTES':<20} {'SOURCE MAIL':<26}"
            spans.append(StyleSpan(text=f"\n{header}\n", style=_STYLE_HEADER))
            for item in items:
                spans.append(StyleSpan(text=f"{item.item_id[:10]:<12} ", style=_STYLE_MUTED))
                spans.append(StyleSpan(text=f"{self._fmt_time(item.due_at):<18} "))
                spans.append(StyleSpan(text=f"{item.action_type:<10} ", style=_STYLE_ACCENT))
                spans.append(StyleSpan(text=f"{item.summary[:30]:<32} "))
                spans.append(StyleSpan(text=f"{item.notes[:18]:<20} ", style=_STYLE_MUTED))
                spans.append(StyleSpan(text=f"{item.mail_id:<26}", style=_STYLE_MUTED))
                spans.append(StyleSpan(text="\n"))
            return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
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
                StyleSpan(text=f"\n{self._t('action.field_mail')}: {action.mail_id}"),
            ]
            return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
        return self._err(self._t("action.usage"))

    async def _find_action(self, item_id: str) -> ActionItem | None:
        for item in await self.service.list_actions():
            if item.item_id == item_id:
                return item
        return None

    async def _cmd_plugin_repo(self, args: list[str]) -> CommandResponse:
        if not args or args[0] == "list":
            repos = self.service.config.plugins.repositories
            spans = [
                StyleSpan(text=self._t("plugin.repo_title", count=len(repos)), style=_STYLE_TITLE),
                StyleSpan(text=f"\n{'NAME':<24} {'URL'}\n", style=_STYLE_HEADER),
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
                    text=f"\n{'PLUGIN':<34} {'VERSION':<10} {'CATEGORIES':<26} {'DESCRIPTION'}\n",
                    style=_STYLE_HEADER,
                ),
            ]
            for _repo, plugin in entries:
                installed = (
                    " [installed]" if market.is_installed(plugin.id, package=plugin.package) else ""
                )
                categories = ",".join(plugin.categories)
                spans.append(StyleSpan(text=f"{plugin.id:<34} "))
                spans.append(StyleSpan(text=f"{plugin.version:<10} ", style=_STYLE_MUTED))
                spans.append(StyleSpan(text=f"{categories:<26} ", style=_STYLE_ACCENT))
                spans.append(StyleSpan(text=f"{plugin.description[:40]}{installed}\n"))
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
            spans = [
                StyleSpan(text=f"{plugin.name or plugin.id} {plugin.version}", style=_STYLE_TITLE),
                StyleSpan(text=f"\n{self._t('plugin.header_id')}: {plugin.id}"),
                StyleSpan(
                    text=f"\n{self._t('plugin.market_categories')}: {', '.join(plugin.categories) or '-'}"
                ),
                StyleSpan(
                    text=f"\n{self._t('plugin.market_description')}: {plugin.description or '-'}"
                ),
                StyleSpan(text=f"\n{self._t('plugin.market_author')}: {plugin.author or '-'}"),
                StyleSpan(text=f"\n{self._t('plugin.market_license')}: {plugin.license or '-'}"),
                StyleSpan(text=f"\n{self._t('plugin.market_source')}: {plugin.source or '-'}"),
                StyleSpan(text=f"\n{self._t('plugin.market_repo')}: {repo.name}"),
                StyleSpan(
                    text=f"\n{self._t('plugin.market_status')}: "
                    f"{self._t('plugin.installed') if market.is_installed(plugin.id, package=plugin.package) else self._t('plugin.not_installed')}"
                ),
            ]
            return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))
        return self._err(self._t("plugin.market_usage"))

    async def _cmd_plugin_install(self, args: list[str]) -> CommandResponse:
        if len(args) != 1:
            return self._err(self._t("plugin.install_usage"))
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
        return self._ok(
            self._t("plugin.installed_ok", plugin_id=plugin.id)
            + f" ({self._t('plugin.restart_note')})"
            + (f"\n{output}" if output else "")
        )

    # -- plugins / adapters / accounts / llms -----------------------------------------------------

    async def _cmd_plugin(self, args: list[str]) -> CommandResponse:
        if args and args[0] == "repo":
            return await self._cmd_plugin_repo(args[1:])
        if args and args[0] == "market":
            return await self._cmd_plugin_market(args[1:])
        if args and args[0] == "install":
            return await self._cmd_plugin_install(args[1:])
        snapshot = self.service.snapshot()
        if not args or args[0] == "list":
            spans = [
                StyleSpan(
                    text=self._t("plugin.title", count=len(snapshot.plugins)), style=_STYLE_TITLE
                ),
                StyleSpan(
                    text=f"\n{'PLUGIN':<30} {'NAME':<24} {'VERSION':<10} {'PROVIDES'}\n",
                    style=_STYLE_HEADER,
                ),
            ]
            for plugin in snapshot.plugins:
                kinds = ",".join(k.value for k in plugin.kinds)
                spans.append(StyleSpan(text=f"{plugin.plugin_id:<30} "))
                spans.append(StyleSpan(text=f"{plugin.name:<24} "))
                spans.append(StyleSpan(text=f"{plugin.version:<10} ", style=_STYLE_MUTED))
                spans.append(StyleSpan(text=f"{kinds}\n", style=_STYLE_ACCENT))
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
            StyleSpan(text=f"\n{'ADAPTER':<32} {'PLUGIN'}\n", style=_STYLE_HEADER),
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
                text=f"\n{'ACCOUNT':<24} {'EMAIL':<36} {'PROVIDER':<24} {'STATUS'}\n",
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
                text=f"\n{'LLM':<20} {'NAME':<20} {'BACKEND':<30} {'MODEL':<20} {'DEFAULT'}\n",
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
        if sub == "show" and rest:
            shown = await self.service.get_draft(rest[0])
            if shown is None:
                return self._err(self._t("reply.draft_not_found", draft_id=rest[0]))
            return self._render_draft(shown)
        if sub == "edit" and len(rest) >= 3:
            draft = await self.service.edit_draft(rest[0], rest[1], " ".join(rest[2:]))
            return self._ok(self._t("reply.edited", draft_id=draft.draft_id))
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
            StyleSpan(text=f"\n{self._t('reply.field_body')}:\n{draft.body}"),
        ]
        return CommandResponse(ok=True, spans=spans, text="".join(s.text for s in spans))

    # -- language -------------------------------------------------------------------------------------

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
            header = f"{'ID':<26} {'':<2} {'URGENCY':<10} {'SUBJECT':<40} {'DELETED':<16}"
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
                if isinstance(value, (list, dict)):
                    value_text = f"{len(value)} items"  # pyright: ignore[reportUnknownArgumentType]
                elif value is None or isinstance(value, (str, int, float, bool)):
                    value_text = "-" if value is None else str(value)[:20]
                elif isinstance(value, bool):
                    value_text = "true" if value else "false"
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
