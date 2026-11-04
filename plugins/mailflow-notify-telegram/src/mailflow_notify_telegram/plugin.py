"""Telegram notifier + gateway provisioner (auto-deploy).

Registers the ``telegram`` notifier and the ``telegram`` gateway provisioner.
Telegram needs no local bot process — the Bot API is the gateway — so the
provisioner's job is to validate the bot token and run the in-process
long-poll bridge that lets chats drive MailFlow commands (see ``gateway.py``).

Requires ``bot_token`` and a configured or subscribed recipient. Delivery
errors are reported to the runtime; mail processing remains independent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mailflow.config import MailFlowConfig, NotifierConfig
from mailflow.domain import ComponentKind, MailRecord
from mailflow.forms import FormField
from mailflow.notifications import DeliveryError, format_mail, targets_of
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

from .gateway import TelegramProvisioner, send_text

logger = logging.getLogger("mailflow.notification.telegram")

PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-notify-telegram",
    name="Telegram Notifier",
    version="0.2.0",
    description="Delivers mail alerts to a Telegram chat via the Bot API",
    kinds=[ComponentKind.NOTIFIER, ComponentKind.GATEWAY_PROVISIONER],
)

# The guided setup asks for the two things Telegram actually needs. The chat
# id is optional: without it the notifier stays silent until the user
# subscribes a chat through the chat commands themselves.
_TELEGRAM_FIELDS: tuple[FormField, ...] = (
    FormField(
        field_id="bot_token",
        kind="password",
        required=True,
        secret=True,
        description_key="tui.extras_telegram_bot_token",
    ),
    FormField(field_id="chat_id", required=False),
    FormField(field_id="api_base", required=False, default="https://api.telegram.org"),
    FormField(
        field_id="admins",
        kind="list",
        required=False,
        description_key="tui.extras_admins",
    ),
)


class TelegramNotifier:
    backend_id = "telegram"

    def __init__(self, config: NotifierConfig) -> None:
        options: dict[str, Any] = getattr(config, "options", None) or {}
        self._token = str(options.get("bot_token", "") or "")
        self._targets = targets_of(config)
        self._api_base = str(options.get("api_base") or "https://api.telegram.org").rstrip("/")

    async def notify(self, record: MailRecord) -> None:
        await self.push_text(format_mail(record))

    async def notify_to_target(
        self, target: str, record: MailRecord, *, text: str | None = None
    ) -> None:
        await self.push_to_target(target, text if text is not None else format_mail(record))

    async def push_text(self, text: str) -> None:
        """Return only after every configured destination accepted the push."""
        if not self._token or not self._targets:
            raise DeliveryError("configuration")
        failure: Exception | None = None
        for target in self._targets:
            try:
                await self.push_to_target(target, text)
            except Exception as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure

    async def push_to_target(self, target: str, text: str) -> None:
        """Push one message to a single chat (per-chat hourly briefing)."""
        if not self._token:
            raise DeliveryError("configuration")
        chat_id = target.partition(":")[2].strip() or target.strip()
        if not chat_id:
            raise DeliveryError("configuration")
        await asyncio.to_thread(
            send_text,
            self._token,
            chat_id,
            text,
            **(
                {"api_base": self._api_base} if self._api_base != "https://api.telegram.org" else {}
            ),
        )


class NotifyPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: MailFlowConfig) -> None:
        registrar.add_notifier("telegram", TelegramNotifier)
        registrar.add_gateway_provisioner("telegram", lambda: TelegramProvisioner())
        registrar.add_form_fields(ComponentKind.NOTIFIER, "telegram", _TELEGRAM_FIELDS)


plugin = NotifyPlugin()

__all__ = ["NotifyPlugin", "TelegramNotifier", "plugin", "targets_of"]
