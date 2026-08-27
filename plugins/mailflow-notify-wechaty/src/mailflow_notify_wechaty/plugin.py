"""WeChat notifier via a WeChaty-style gateway backed by the PAD protocol.

Component id ``wechaty``. MailFlow talks to a small HTTP gateway in front
of the pad-protocol client (WeChatFerry / PaPad / any bridge implementing
the same two endpoints):

- ``POST {gateway_url}/send``  body ``{"to": {"type": "contact"|"room",
  "name": …}, "text": …}``
- ``GET  {gateway_url}/health``

## Login: pad protocol required

The official WeChaty ``puppet-wechat`` (web protocol) is defunct and
carries a ban risk. Use a **pad protocol** client instead:

1. Install and run **WeChatFerry** (github.com/lich0821/WeChatFerry) with
   the bundled WeChat 3.9.x client, or a PaPad service; scan the QR to
   log your bot account in.
2. Run a thin bridge exposing the two endpoints above (the repository
   ships ``examples/wechaty-gateway.js`` as a reference).
3. Fill this notifier's ``gateway_url``/``token`` and press Check in the
   Bots tab; the status column reports the login session.

Only use a disposable account: non-official WeChat protocols are against
ToS and any account may be banned.

Options: ``gateway_url``, ``token`` (sent as ``Authorization: Bearer``),
``targets`` list of ``contact:<name>`` / ``room:<topic>``.
Missing configuration skips delivery; failures are logged.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from mailflow.config import NotifierConfig
from mailflow.domain import ComponentKind, MailRecord
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

logger = logging.getLogger("mailflow.notify.wechaty")


class WechatyNotifier:
    backend_id = "wechaty"

    def __init__(self, config: NotifierConfig) -> None:
        self._url = str(config.options.get("gateway_url", "")).rstrip("/")
        self._token = str(config.options.get("token", ""))
        raw_targets: list[Any] = list(config.options.get("targets") or [])
        self._targets: list[tuple[str, str]] = []
        for entry in raw_targets:
            text = str(entry).strip()
            kind, _, name = text.partition(":")
            kind = kind.strip().lower()
            if kind in ("contact", "room") and name.strip():
                self._targets.append((kind, name.strip()))

    async def notify(self, record: MailRecord) -> None:
        if not self._url or not self._targets:
            logger.warning("wechaty notifier: gateway_url/targets not configured; skipping")
            return
        text = format_message(record)
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            for kind, name in self._targets:
                payload = {
                    "to": {"type": "room" if kind == "room" else "contact", "name": name},
                    "text": text,
                }
                try:
                    response = await client.post(f"{self._url}/send", json=payload, headers=headers)
                    response.raise_for_status()
                except Exception as exc:
                    logger.warning("wechaty delivery to %s:%s failed: %s", kind, name, exc)


def format_message(record: MailRecord) -> str:
    sender = record.mail.sender.display or record.mail.sender.address
    lines = [
        f"[MailFlow] {record.effective_urgency.value.upper()} — {record.mail.subject}",
        f"From: {sender}",
    ]
    if record.summary:
        lines.append(record.summary[:500])
    attachments = [a.filename for a in record.mail.attachments if a.filename]
    if attachments:
        shown = ", ".join(attachments[:4])
        more = f" (+{len(attachments) - 4})" if len(attachments) > 4 else ""
        lines.append(f"Attachments: {shown}{more}")
    return "\n".join(lines)


PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-notify-wechaty",
    name="WeChat Notifier (WeChaty)",
    version="0.1.0",
    description="Pushes mail alerts to WeChat contacts/rooms via a WeChaty HTTP gateway",
    kinds=[ComponentKind.NOTIFIER],
)


class NotifierPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        registrar.add_notifier("wechaty", WechatyNotifier)


plugin = NotifierPlugin()

__all__ = ["NotifierPlugin", "WechatyNotifier", "plugin"]
