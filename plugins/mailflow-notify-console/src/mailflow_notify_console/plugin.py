"""Console notifications for MailFlow's temporary in-app conversation."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from mailflow.domain import ComponentKind, MailRecord
from mailflow.i18n import I18n
from mailflow.notifications import current_delivery, format_mail
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

logger = logging.getLogger("mailflow.notification.console")

PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-notify-console",
    name="Console Notifier",
    version="0.1.0",
    description="Delivers mail, reminders and summaries to the TUI console conversation",
    kinds=[ComponentKind.NOTIFIER],
)


class ConsoleNotifier:
    def __init__(self, config: Any = None) -> None:
        self._publish: Callable[[str, str, list[dict[str, str]], str], Awaitable[None]] | None = (
            None
        )
        self._t: Callable[..., str] = I18n().t

    def bind_console(
        self,
        publish: Callable[[str, str, list[dict[str, str]], str], Awaitable[None]],
        translate: Callable[..., str],
    ) -> None:
        self._publish, self._t = publish, translate

    async def notify(self, record: MailRecord) -> None:
        await self._send(
            format_mail(record, self._t),
            references=[
                {
                    "kind": "mail",
                    "id": record.record_id,
                    "title": record.mail.subject,
                }
            ],
            kind="mail",
        )

    async def push_text(self, text: str) -> None:
        await self._send(text)

    async def _send(
        self,
        text: str,
        *,
        references: list[dict[str, str]] | None = None,
        kind: str = "notification",
    ) -> None:
        context = current_delivery()
        if self._publish is None:
            logger.warning("%s", text)
            return
        await self._publish(
            text,
            context.kind if context.delivery_id else kind,
            context.references or references or [],
            context.delivery_id,
        )


class NotifyPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        registrar.add_notifier("console", ConsoleNotifier)


plugin = NotifyPlugin()

__all__ = ["ConsoleNotifier", "NotifyPlugin", "plugin"]
