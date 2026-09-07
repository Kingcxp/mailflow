"""Tencent OpenClaw WeChat (ClawBot / iLink) notifier.

Component id ``openclaw-weixin``. Talks to an OpenClaw gateway that has
the official ``@tencent-weixin/openclaw-weixin`` channel plugin enabled.

Contract: Gateway HTTP hooks (``https://docs.openclaw.ai/gateway/config-hooks``).
Each notification POSTs to ``{base_url}{endpoint}`` (default ``/hooks/agent``)
with ``Authorization: Bearer <token>`` and the hook agent payload::

    {"message": <formatted mail>, "channel": "openclaw-weixin", "to": <id>}

``channel``/``to`` request direct announce delivery to the WeChat channel.
Note this submits an agent turn — the OpenClaw agent sees the mail text and
OpenClaw may transform it before delivery — it is not a raw message relay.

Prerequisites on the gateway side: ``hooks.enabled: true``, a dedicated
``hooks.token`` (this notifier's ``api_key``), and ``"openclaw-weixin"``
listed among the enabled channels.

Options: ``base_url`` (e.g. ``http://127.0.0.1:18789``),
``api_key``/``api_key_env`` (hook token), ``endpoint`` (default
``/hooks/agent``; adjust when ``hooks.path`` differs), ``targets`` list of
WeChat user ids (``xxx@im.wechat``).
Experimental: the upstream API is still evolving; delivery failures are
logged, never raised.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from mailflow.config import NotifierConfig
from mailflow.domain import ComponentKind, MailRecord
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

logger = logging.getLogger("mailflow.notify.openclaw")


class OpenClawWeixinNotifier:
    backend_id = "openclaw-weixin"

    def __init__(self, config: NotifierConfig) -> None:
        self._url = str(config.options.get("base_url", "")).rstrip("/")
        self._endpoint = str(config.options.get("endpoint", "/hooks/agent"))
        self._api_key = str(config.options.get("api_key", ""))
        env_name = config.options.get("api_key_env")
        if not self._api_key and env_name:
            self._api_key = os.environ.get(str(env_name), "")
        raw_targets: list[Any] = list(config.options.get("targets") or [])
        self._targets = [str(entry).strip() for entry in raw_targets if str(entry).strip()]

    async def notify(self, record: MailRecord) -> None:
        if not self._url or not self._targets or not self._api_key:
            logger.warning(
                "openclaw-weixin notifier: base_url/api_key/targets incomplete; skipping"
            )
            return
        text = format_message(record)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        url = f"{self._url}{self._endpoint}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            for target in self._targets:
                payload = {"message": text, "channel": "openclaw-weixin", "to": target}
                try:
                    response = await client.post(url, json=payload, headers=headers)
                    response.raise_for_status()
                except Exception as exc:
                    logger.warning("openclaw delivery to %s failed: %s", target, exc)


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
    plugin_id="mailflow-notify-openclaw-weixin",
    name="WeChat Notifier (OpenClaw)",
    version="0.1.0",
    description=(
        "Pushes mail alerts via Tencent's OpenClaw WeChat channel "
        "(ClawBot/iLink) through OpenClaw gateway HTTP hooks"
    ),
    kinds=[ComponentKind.NOTIFIER],
)


class NotifierPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        registrar.add_notifier("openclaw-weixin", OpenClawWeixinNotifier)


plugin = NotifierPlugin()

__all__ = ["NotifierPlugin", "OpenClawWeixinNotifier", "plugin"]
