"""Service-owned, temporary console messages. No transcript is written to storage."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from mailflow.contracts import ConsoleNotifierBinding
from mailflow.domain import CommandResponse, ComponentKind, SmartActionResult, utcnow
from mailflow.errors import error_text, translated_error
from mailflow.notifications import option_enabled
from mailflow.permissions import PermissionContext, current_context, permission_scope, require_admin

if TYPE_CHECKING:
    from mailflow.config import NotifierConfig
    from mailflow.contracts import Notifier
    from mailflow.service import MailFlowService

logger = logging.getLogger("mailflow.notification.console")
MESSAGE_LIMIT = 1000


class ConsoleMessage(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    sequence: int = 0
    revision: int = 0
    channel: str = "console"
    role: Literal["user", "bot", "notification", "system"] = "bot"
    kind: str = "message"
    text: str = ""
    at: datetime = Field(default_factory=utcnow)
    state: str = "done"
    markdown: bool = True
    request_id: str = ""
    reply_to: str = ""
    references: list[dict[str, str]] = Field(default_factory=list)
    files: list[dict[str, Any]] = Field(default_factory=list)
    owner: str = Field(default="", exclude=True)


@dataclass
class ConsoleTurn:
    """Presentation for one temporary turn; execution stays in the shared chat router."""

    quote: ConsoleMessage | None = None
    progress: Callable[..., None] | None = None
    result: SmartActionResult | None = None
    response: CommandResponse | None = None


class ConsoleInbox:
    """A bounded notification feed plus private conversations for one service run."""

    def __init__(self, service: MailFlowService) -> None:
        self.service = service
        self.runtime_id = uuid4().hex
        self.revision = 0
        self._feeds: dict[tuple[str, str], deque[ConsoleMessage]] = {}
        self._cleared: dict[tuple[str, str], int] = {}
        self._muted: dict[tuple[str, str], list[tuple[int, int | None]]] = {}
        self._deliveries: OrderedDict[str, None] = OrderedDict()
        self.requests: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._submitted: OrderedDict[tuple[str, str, str], ConsoleMessage] = OrderedDict()
        self._submission_lock = asyncio.Lock()
        self._readers: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._updates: set[asyncio.Task[Any]] = set()
        self._files: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._file_bytes = 0
        self.closed = False

    @staticmethod
    def owner(context: PermissionContext | None = None) -> str:
        context = context or current_context()
        if context.provider == "console":
            return context.chat_id
        return sha256(
            repr((context.provider, context.sender, context.chat_id)).encode()
        ).hexdigest()

    def channel(self, channel: str = "") -> str:
        channels = [n.notifier_id for n in self.service.config.notifiers if n.provider == "console"]
        virtual = "console"
        used = {n.notifier_id for n in self.service.config.notifiers}
        while virtual in used and virtual not in channels:
            virtual += "-local"
        selected = channel or (channels[0] if channels else virtual)
        if selected not in (channels or [virtual]):
            raise translated_error(ValueError, self.service.t, "console.channel_missing")
        return selected

    def bind(self, notifiers: list[Notifier], configs: list[NotifierConfig]) -> None:
        for notifier, config in zip(notifiers, configs, strict=True):
            if config.provider != "console" or not isinstance(notifier, ConsoleNotifierBinding):
                continue

            async def publish(
                text: str,
                kind: str,
                references: list[dict[str, str]],
                delivery_id: str,
                *,
                channel: str = config.notifier_id,
            ) -> None:
                if self.closed:
                    raise translated_error(RuntimeError, self.service.t, "console.stopped")
                if delivery_id and delivery_id in self._deliveries:
                    return
                await self.append(
                    ConsoleMessage(
                        channel=channel,
                        role="notification",
                        kind=kind,
                        text=text,
                        references=references,
                        markdown=False,
                    )
                )
                if delivery_id:
                    self._deliveries[delivery_id] = None
                    while len(self._deliveries) > MESSAGE_LIMIT * 4:
                        self._deliveries.popitem(last=False)
                if self.service.config.logging.console:
                    logger.warning("%s", text)

            notifier.bind_console(publish, self.service.t)

    async def append(self, message: ConsoleMessage) -> ConsoleMessage:
        self.revision += 1
        message.sequence = message.revision = self.revision
        feed = self._feeds.setdefault((message.channel, message.owner), deque(maxlen=MESSAGE_LIMIT))
        feed.append(message)
        await self.changed(message.channel, message.owner)
        return message.model_copy(deep=True)

    async def changed(self, channel: str, owner: str = "") -> None:
        # Only a wake-up travels on the shared bus; never private message bodies.
        await self.service.events.emit(
            "console.changed",
            channel=channel,
            owner=owner,
            revision=self.revision,
            runtime_id=self.runtime_id,
        )

    def visible_event(self, payload: dict[str, Any]) -> bool:
        return not payload.get("owner") or payload["owner"] == self.owner()

    def channels(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        active = {c.notifier_id: n for n, c in self.service.runtime.notification_channels()}
        for config in self.service.config.notifiers:
            if config.provider != "console":
                continue
            reason = ""
            if not config.enabled:
                reason = "console.notifications_disabled"
            elif not isinstance(active.get(config.notifier_id), ConsoleNotifierBinding):
                reason = "console.plugin_unavailable"
            elif not self.service.config.general.mail_notifications:
                reason = "console.mail_global_off"
            elif not option_enabled(config.options, "mail_notifications"):
                reason = "console.mail_channel_off"
            result.append(
                {
                    "id": config.notifier_id,
                    "configured": True,
                    "enabled": config.enabled,
                    "reason": self.service.t(reason) if reason else "",
                }
            )
        return result or [
            {
                "id": self.channel(),
                "configured": False,
                "enabled": False,
                "reason": self.service.t("console.not_configured"),
            }
        ]

    async def read(
        self,
        channel: str = "",
        *,
        after: int = 0,
        before: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        channel = self.channel(channel)
        owner = self.owner()
        cutoff = self._cleared.get((channel, owner), 0)
        rows = sorted(
            (
                row
                for key in ((channel, ""), (channel, owner))
                for row in self._feeds.get(key, ())
                if row.sequence > cutoff
                and (
                    row.owner
                    or not any(
                        start < row.sequence and (end is None or row.sequence <= end)
                        for start, end in self._muted.get((channel, owner), ())
                    )
                )
            ),
            key=lambda row: row.sequence,
        )[-MESSAGE_LIMIT:]
        eligible = [
            row
            for row in rows
            if (not after or row.revision > after) and (not before or row.sequence < before)
        ]
        limit = max(1, min(limit, MESSAGE_LIMIT))
        page = eligible if after else eligible[-limit:]
        return {
            "runtime_id": self.runtime_id,
            "channel": channel,
            "cursor": self.revision,
            "messages": [r.model_dump(mode="json") for r in page],
            "has_more": len(eligible) > len(page),
            "oldest": rows[0].sequence if rows else 0,
            "cleared": cutoff,
            "channels": self.channels(),
            "command_prefix": self.service.command_prefix(),
            "busy": (channel, owner) in self._readers,
            "truncated": len(rows) == MESSAGE_LIMIT,
            "subscribed": self.subscribed(channel),
        }

    def subscribed(self, channel: str) -> bool:
        spans = self._muted.get((channel, self.owner()), [])
        return not spans or spans[-1][1] is not None

    async def chat_control(self, channel: str, sub: str, args: list[str]) -> str:
        channel = self.channel(channel)
        state = next(c for c in self.channels() if c["id"] == channel)
        if sub in {"subscribe", "unsubscribe"}:
            if sub == "subscribe" and (not state["configured"] or not state["enabled"]):
                return state["reason"]
            spans = self._muted.setdefault((channel, self.owner()), [])
            if sub == "unsubscribe" and self.subscribed(channel):
                spans.append((self.revision, None))
            elif sub == "subscribe" and not self.subscribed(channel):
                spans[-1] = (spans[-1][0], self.revision)
            await self.changed(channel, self.owner())
            return self.service.t(
                "console.subscribed" if sub == "subscribe" else "console.unsubscribed"
            )
        if sub == "hourly":
            arg = args[0].lower() if args else ""
            if arg not in {"", "on", "off"}:
                return self.service.t("chat.hourly_usage")
            if not state["configured"] or not state["enabled"]:
                return state["reason"]
            index = next(
                i for i, n in enumerate(self.service.config.notifiers) if n.notifier_id == channel
            )
            config = self.service.config.notifiers[index]
            if arg:
                await self.service.update_config_entry(
                    "notifiers",
                    index,
                    {
                        "options": {**config.options, "hourly_summary": arg == "on"},
                    },
                )
                config = self.service.config.notifiers[index]
            reason = (
                self.service.t("chat.hourly_global_off")
                if not self.service.config.general.hourly_summary
                else self.service.t("chat.hourly_channel_off")
                if not option_enabled(config.options, "hourly_summary", False)
                else self.service.t("console.unsubscribed")
                if not self.subscribed(channel)
                else self.service.t("mail.search_no_llm")
                if not self.service.config.llms
                else ""
            )
            return (
                self.service.t("chat.hourly_effective_off", reason=reason)
                if reason
                else self.service.t("chat.hourly_status_on")
            )
        return self.service.t(
            "console.status",
            channel=channel,
            state=state["reason"] or self.service.t("console.ready"),
            subscription=self.service.t(
                "console.subscribed" if self.subscribed(channel) else "console.unsubscribed"
            ),
        )

    def identity(self, channel: str) -> PermissionContext:
        base = current_context()
        return PermissionContext(
            sender=base.sender,
            provider="console",
            instance_id=channel,
            chat_id=self.owner(base),
            is_admin=base.is_admin,
        )

    def find(self, channel: str, message_id: str) -> ConsoleMessage:
        owner = self.owner()
        cutoff = self._cleared.get((channel, owner), 0)
        for key in ((channel, ""), (channel, owner)):
            for message in self._feeds.get(key, ()):
                if message.id == message_id and message.sequence > cutoff:
                    return message.model_copy(deep=True)
        raise translated_error(ValueError, self.service.t, "console.quote_missing")

    def _update(self, message: ConsoleMessage, **values: Any) -> None:
        self.revision += 1
        for name, value in values.items():
            setattr(message, name, value)
        message.revision = self.revision

    async def submit(
        self,
        text: str,
        *,
        channel: str = "",
        request_id: str,
        quote_id: str = "",
    ) -> ConsoleMessage:
        async with self._submission_lock:
            return await self._submit(
                text, channel=channel, request_id=request_id, quote_id=quote_id
            )

    async def _submit(
        self,
        text: str,
        *,
        channel: str,
        request_id: str,
        quote_id: str,
    ) -> ConsoleMessage:
        if self.closed:
            raise translated_error(RuntimeError, self.service.t, "console.stopped")
        channel = self.channel(channel)
        owner, identity = self.owner(), self.identity(channel)
        key = (channel, owner, request_id)
        if key in self._submitted:
            return self._submitted[key].model_copy(deep=True)
        if (channel, owner) in self._readers:
            raise translated_error(RuntimeError, self.service.t, "assistant.busy")
        quote = self.find(channel, quote_id) if quote_id else None
        text = text.strip()
        if not text:
            raise translated_error(ValueError, self.service.t, "console.empty_input")
        if not request_id:
            raise translated_error(ValueError, self.service.t, "console.request_required")
        message = await self.append(
            ConsoleMessage(
                channel=channel,
                owner=owner,
                role="user",
                text=text,
                markdown=False,
                request_id=request_id,
                reply_to=quote_id,
            )
        )
        self._submitted[key] = message
        while len(self._submitted) > MESSAGE_LIMIT:
            self._submitted.popitem(last=False)
        mention = re.match(r"^@mailflow(?:\s+|$)", text, re.IGNORECASE)
        instruction = text[mention.end() :].strip() if mention else text
        prefix = self.service.command_prefix()
        command = (
            text.startswith(prefix)
            and text[len(prefix) :].strip().partition(" ")[0].split("@", 1)[0].casefold()
            == "mailflow"
        )
        if not command and not mention and quote is None:
            return message
        if not instruction:
            await self.append(
                ConsoleMessage(
                    channel=channel,
                    owner=owner,
                    role="system",
                    text=self.service.t("console.mention_hint"),
                    markdown=False,
                )
            )
            return message
        # Reserve the conversation before yielding while mounting the reply.
        task = asyncio.create_task(
            self._consume(
                channel,
                owner,
                identity,
                instruction,
                message,
                quote,
                bool(mention or quote is not None),
            ),
            name="console-forward",
        )
        self._readers[(channel, owner)] = task
        return message

    async def _consume(
        self,
        channel: str,
        owner: str,
        identity: PermissionContext,
        text: str,
        message: ConsoleMessage,
        quote: ConsoleMessage | None,
        mentioned: bool,
    ) -> None:
        reply = ConsoleMessage(
            channel=channel,
            owner=owner,
            request_id=message.request_id,
            reply_to=message.id,
            state="running",
            text=self.service.t("console.processing"),
        )
        turn = ConsoleTurn(quote=quote)

        def progress(_stage: str, _done: int, _total: int, detail: Any) -> None:
            if self.closed or reply.state != "running":
                return
            if isinstance(detail, (tuple, list)) and len(detail) == 2:
                name, params = detail
                params = {k: v for k, v in params.items() if k not in {"args", "result"}}
                self._update(reply, text=self.service.t("tui." + str(name), **params))
                update = asyncio.create_task(self.changed(channel, owner))
                self._updates.add(update)
                update.add_done_callback(self._updates.discard)

        turn.progress = progress
        try:
            await self.append(reply)
            from mailflow.chat_files import file_sender_scope

            async def file_sender(file: dict[str, Any]) -> None:
                reference = uuid4().hex
                self._files[reference] = file
                self._file_bytes += len(str(file.get("data", "")))
                while self._file_bytes > 64 * 1024 * 1024 and self._files:
                    _, dropped = self._files.popitem(last=False)
                    self._file_bytes -= len(str(dropped.get("data", "")))
                reply.files.append(
                    {
                        "id": reference,
                        "filename": str(file["filename"]),
                        "content_type": str(file.get("content_type", "application/octet-stream")),
                    }
                )

            with permission_scope(identity), file_sender_scope(file_sender):
                if self.service.commands is None:
                    from mailflow.commands import CommandRouter

                    CommandRouter(self.service)
                async for result in self.service.chat_command_stream(
                    text,
                    context=identity,
                    sender=identity.sender,
                    provider="console",
                    instance_id=channel,
                    chat_id=identity.chat_id,
                    chat_type="group",
                    request_id=message.request_id,
                    mentioned=mentioned,
                    turn=turn,
                ):
                    if result:
                        self._update(
                            reply, text="\n\n".join(result) if isinstance(result, list) else result
                        )
                        await self.changed(channel, owner)
            references: list[dict[str, str]] = []
            if turn.result is not None:
                references = [
                    {"kind": "mail", "id": r.record_id, "title": r.mail.subject}
                    for r in turn.result.records
                ] + [
                    {"kind": "action", "id": a.item_id, "title": a.summary}
                    for a in turn.result.actions
                ]
            elif turn.response is not None:
                references = turn.response.references
            state = (
                "cancelled"
                if reply.text == self.service.t("chat.cancelled")
                else "failed"
                if turn.response is not None and not turn.response.ok
                else "done"
            )
            self._update(
                reply,
                state=state,
                references=references,
                markdown=turn.response is None and mentioned,
            )
        except asyncio.CancelledError:
            self._update(reply, state="cancelled", text=self.service.t("chat.cancelled"))
        except Exception as exc:
            self._update(
                reply, state="failed", text=error_text(exc, self.service.t), markdown=False
            )
        finally:
            self._readers.pop((channel, owner), None)
            if not self.closed:
                await self.changed(channel, owner)

    async def cancel(self, channel: str = "") -> None:
        channel = self.channel(channel)
        identity = self.identity(channel)
        actor = self.service._chat_actor("console", channel, identity.chat_id, identity.sender)
        reader = self._readers.get((channel, self.owner()))
        if reader is not None and not self.service._chat_running.get(actor):
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            self._readers.pop((channel, self.owner()), None)
            reply = self.service.t("chat.cancelled")
        else:
            with permission_scope(identity):
                reply = await self.service.command_dispatch(
                    self.service.command_prefix() + "mailflow cancel",
                    context=identity,
                    provider="console",
                    instance_id=channel,
                    sender=identity.sender,
                    chat_id=identity.chat_id,
                    chat_type="group",
                )
        await self.append(
            ConsoleMessage(
                channel=channel,
                owner=self.owner(),
                role="system",
                markdown=False,
                text=str(reply or self.service.t("chat.cancel_idle")),
            )
        )

    async def clear(self, channel: str = "") -> None:
        channel = self.channel(channel)
        owner = self.owner()
        if (channel, owner) in self._readers:
            raise translated_error(RuntimeError, self.service.t, "console.stop_before_clear")
        identity = self.identity(channel)
        actor = self.service._chat_actor("console", channel, identity.chat_id, identity.sender)
        with permission_scope(identity):
            confirmation = self.service._assistant_confirmations.pop(actor, None)
            if confirmation is not None:
                await self.service.cancel_smart_plan(confirmation[1].plan_id)
            self.service._assistant_chats.pop(actor, None)
            if self.service.commands is not None:
                self.service.commands.clear_console_selection()
            for draft in (
                await self.service.list_drafts(state="prepared") if identity.is_admin else []
            ):
                if draft.owner == self.service.workflows_actor():
                    await self.service.cancel_reply(draft.draft_id)
        for row in self._feeds.pop((channel, owner), ()):
            for file in row.files:
                dropped = self._files.pop(file["id"], {})
                self._file_bytes -= len(str(dropped.get("data", "")))
        self._cleared[(channel, owner)] = self.revision
        # Old network retries must not re-execute a cleared conversation.
        for key in [key for key in self._submitted if key[:2] == (channel, owner)]:
            self._submitted[key].text = ""
            cached = self.requests.get(sha256(f"{actor}:{key[2]}".encode()).hexdigest())
            if cached is not None:
                cached["reply"] = ""
        if self.subscribed(channel):
            self._muted.pop((channel, owner), None)
        else:
            self._muted[(channel, owner)] = [(self.revision, None)]
        await self.changed(channel, owner)

    async def enable(self, channel: str = "") -> None:
        require_admin(self.service.t)
        channel = self.channel(channel)
        if not self.service.registry.has(ComponentKind.NOTIFIER, "console"):
            raise translated_error(RuntimeError, self.service.t, "console.plugin_unavailable")
        for index, config in enumerate(self.service.config.notifiers):
            if config.notifier_id == channel and config.provider == "console":
                if not config.enabled:
                    await self.service.update_config_entry("notifiers", index, {"enabled": True})
                return
        await self.service.add_config_entry(
            "notifiers",
            {
                "notifier_id": channel,
                "provider": "console",
                "minimum_urgency": "important",
            },
        )

    async def upload(self, channel: str, file: dict[str, Any]) -> dict[str, Any]:
        channel = self.channel(channel)
        with permission_scope(self.identity(channel)):
            reference = await self.service.workspace_call("upload", file)
        await self.append(
            ConsoleMessage(
                channel=channel,
                owner=self.owner(),
                role="user",
                kind="file",
                markdown=False,
                text=self.service.t(
                    "console.uploaded", name=reference["filename"], id=reference["file_id"]
                ),
            )
        )
        return reference

    async def file(self, channel: str, message_id: str, file_id: str) -> dict[str, Any]:
        require_admin(self.service.t)
        message = self.find(self.channel(channel), message_id)
        if not any(file["id"] == file_id for file in message.files) or file_id not in self._files:
            raise translated_error(ValueError, self.service.t, "experience.file_unavailable")
        return dict(self._files[file_id])

    async def close(self) -> None:
        self.closed = True
        tasks = [*self._readers.values(), *self._updates]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._readers.clear()
        self._updates.clear()
        self._files.clear()
        self._file_bytes = 0
        self.requests.clear()
        self._submitted.clear()
        self._feeds.clear()
        self._cleared.clear()
        self._muted.clear()
        self._deliveries.clear()
