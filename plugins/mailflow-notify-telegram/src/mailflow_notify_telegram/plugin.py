"""Telegram notifier + gateway provisioner (auto-deploy).

Registers the ``telegram`` notifier and the ``telegram`` gateway provisioner.
Telegram needs no local bot process — the Bot API is the gateway — so the
provisioner's job is to validate the bot token and run the in-process
long-poll bridge that lets chats drive MailFlow commands (see ``gateway.py``).

Requires ``bot_token`` and ``chat_id`` in the notifier options. When either is
missing the notifier logs a hint and skips — the pipeline must never fail
because a channel is not configured.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mailflow.config import MailFlowConfig, NotifierConfig
from mailflow.domain import ComponentKind, MailRecord
from mailflow.forms import FormField
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
        description_key="telegram_bot_token",
    ),
    FormField(
        field_id="admins",
        kind="list",
        required=False,
        description_key="admins",
    ),
)


def _bare_id(entry: str) -> str:
    """The chat id inside a ``user:<id>`` / ``chat:<id>`` target (or as-is)."""
    _, _, tail = entry.partition(":")
    return (tail or entry).strip()


def targets_of(config: NotifierConfig) -> list[str]:
    """Delivery targets, with ``chat_id`` folded in as the single-destination
    form of the same setting.

    Deduplication is on the bare chat id: a chat listed as ``user:42`` in
    targets must not also receive the alert through ``chat_id = 42``.
    """
    raw: list[Any] = list(config.options.get("targets") or [])
    targets = [str(entry).strip() for entry in raw if str(entry).strip()]
    chat_id = str(config.options.get("chat_id") or "").strip()
    if chat_id and chat_id not in {_bare_id(entry) for entry in targets}:
        targets.append(chat_id)
    return targets


class TelegramNotifier:
    backend_id = "telegram"

    def __init__(self, config: NotifierConfig) -> None:
        options: dict[str, Any] = getattr(config, "options", None) or {}
        self._token = str(options.get("bot_token", "") or "")
        self._targets = targets_of(config)

    async def notify(self, record: MailRecord) -> None:
        if not self._token or not self._targets:
            logger.warning(
                "telegram notifier skipped: set bot_token and chat_id in "
                "[notifiers.options] (record %s)",
                record.record_id,
            )
            return
        urgency = record.effective_urgency
        text = f"[{urgency.value}] {record.mail.subject}\n{record.summary}"
        await self._send_all(text)

    async def push_text(self, text: str) -> None:
        """Plain-text push (schedule reminders, daily digest)."""
        if not self._token or not self._targets:
            return
        await self._send_all(text)

    async def push_to_target(self, target: str, text: str) -> None:
        """Push one message to a single chat (per-chat hourly briefing)."""
        if not self._token:
            return
        chat_id = target.partition(":")[2].strip() or target.strip()
        if not chat_id:
            return
        await asyncio.to_thread(send_text, self._token, chat_id, text)

    async def _send_all(self, text: str) -> None:
        for target in self._targets:
            try:
                await asyncio.to_thread(send_text, self._token, _bare_id(target), text)
            except Exception as exc:
                # one unreachable chat must not abort delivery to the rest
                logger.warning("telegram delivery to %s failed: %s", _bare_id(target), exc)


class NotifyPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: MailFlowConfig) -> None:
        registrar.add_notifier("telegram", TelegramNotifier)
        registrar.add_gateway_provisioner("telegram", lambda: TelegramProvisioner())
        registrar.add_form_fields(ComponentKind.NOTIFIER, "telegram", _TELEGRAM_FIELDS)


plugin = NotifyPlugin()

__all__ = ["NotifyPlugin", "TelegramNotifier", "plugin", "targets_of"]
