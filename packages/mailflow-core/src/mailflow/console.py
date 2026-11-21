"""Service-owned, temporary console messages. No transcript is written to storage."""

from __future__ import annotations

import logging
from collections import OrderedDict, deque
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from mailflow.contracts import ConsoleNotifierBinding
from mailflow.domain import utcnow
from mailflow.errors import translated_error
from mailflow.notifications import option_enabled
from mailflow.permissions import PermissionContext, current_context

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
    request_id: str = ""
    reply_to: str = ""
    references: list[dict[str, str]] = Field(default_factory=list)
    files: list[dict[str, Any]] = Field(default_factory=list)
    owner: str = Field(default="", exclude=True)


class ConsoleInbox:
    """A bounded notification feed plus private conversations for one service run."""

    def __init__(self, service: MailFlowService) -> None:
        self.service = service
        self.runtime_id = uuid4().hex
        self.revision = 0
        self._feeds: dict[tuple[str, str], deque[ConsoleMessage]] = {}
        self._cleared: dict[tuple[str, str], int] = {}
        self._deliveries: OrderedDict[str, None] = OrderedDict()
        self.closed = False

    @staticmethod
    def owner(context: PermissionContext | None = None) -> str:
        context = context or current_context()
        return sha256(
            repr((context.provider, context.sender, context.chat_id)).encode()
        ).hexdigest()

    def channel(self, channel: str = "") -> str:
        channels = [n.notifier_id for n in self.service.config.notifiers if n.provider == "console"]
        selected = channel or (channels[0] if channels else "console")
        if selected not in (channels or ["console"]):
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
                "id": "console",
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
        }

    async def close(self) -> None:
        self.closed = True
        self._feeds.clear()
        self._cleared.clear()
        self._deliveries.clear()
