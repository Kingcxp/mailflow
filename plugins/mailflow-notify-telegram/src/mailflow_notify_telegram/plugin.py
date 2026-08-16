"""Telegram notifier: pushes already-computed mail alerts to a Telegram chat
via the Bot API (urllib only, no extra dependencies).

Requires ``bot_token`` and ``chat_id`` in the notifier options. When either
is missing the notifier logs a hint and skips — the pipeline must never fail
because a channel is not configured.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mailflow.domain import ComponentKind, MailRecord
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

logger = logging.getLogger("mailflow.notification.telegram")

PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-notify-telegram",
    name="Telegram Notifier",
    version="0.1.0",
    description="Delivers mail alerts to a Telegram chat via the Bot API",
    kinds=[ComponentKind.NOTIFIER],
)

_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    def __init__(self, config: Any) -> None:
        options: dict[str, Any] = getattr(config, "options", None) or {}
        self._token = str(options.get("bot_token", "") or "")
        self._chat_id = str(options.get("chat_id", "") or "")

    async def notify(self, record: MailRecord) -> None:
        if not self._token or not self._chat_id:
            logger.warning(
                "telegram notifier skipped: set bot_token and chat_id in "
                "[notifiers.options] (record %s)",
                record.record_id,
            )
            return
        urgency = record.effective_urgency
        text = f"[{urgency.value}] {record.mail.subject}\n{record.summary}"
        await asyncio.to_thread(self._post, text)

    def _post(self, text: str) -> None:
        payload = urlencode({"chat_id": self._chat_id, "text": text}).encode()
        request = Request(
            _API.format(token=self._token),
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(request, timeout=10) as response:
            response.read()


class NotifyPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        registrar.add_notifier(
            "telegram", lambda notifier_config: TelegramNotifier(notifier_config)
        )


plugin = NotifyPlugin()

__all__ = ["NotifyPlugin", "TelegramNotifier", "plugin"]
