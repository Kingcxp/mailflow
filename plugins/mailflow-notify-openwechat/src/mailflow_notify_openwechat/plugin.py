"""MailFlow plugin: openwechat-based WeChat gateway (auto-deploy).

Registers the ``openwechat`` gateway provisioner (scan-to-login, no
platform token) and the ``openwechat`` notifier, which posts to the
bridge's ``POST /send`` endpoint. The bridge is a Go binary built on
install; see ``gateway.py`` for the contract.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from mailflow.config import MailFlowConfig, NotifierConfig
from mailflow.domain import ComponentKind, MailRecord
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

from .gateway import OpenWechatProvisioner

logger = logging.getLogger("mailflow.plugins.openwechat")


class OpenWechatNotifier:
    backend_id = "openwechat"

    def __init__(self, config: NotifierConfig) -> None:
        self._url = str(config.options.get("gateway_url", "")).rstrip("/")
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
            logger.warning("openwechat notifier: gateway_url/targets not configured; skipping")
            return
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
        text = "\n".join(lines)
        async with httpx.AsyncClient(timeout=20.0) as client:
            for kind, name in self._targets:
                payload = {
                    "to": {"type": "room" if kind == "room" else "contact", "name": name},
                    "text": text,
                }
                try:
                    response = await client.post(f"{self._url}/send", json=payload)
                    response.raise_for_status()
                except Exception as exc:
                    logger.warning("openwechat delivery to %s:%s failed: %s", kind, name, exc)


class OpenWechatPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PluginInfo(
            plugin_id="mailflow-notify-openwechat",
            name="openwechat (WeChat scan-to-login)",
            version="0.1.0",
            description="WeChat gateway with QR scan-to-login, no platform token",
            kinds=[ComponentKind.NOTIFIER, ComponentKind.GATEWAY_PROVISIONER],
        )

    def mailflow_register(self, registrar: PluginRegistrar, config: MailFlowConfig) -> None:
        registrar.add_notifier("openwechat", OpenWechatNotifier)
        registrar.add_gateway_provisioner("openwechat", lambda: OpenWechatProvisioner())
        # component registration is routine startup detail, not something
        # the user needs at INFO — it fires on every app start whether or
        # not any instance is deployed
        logger.debug("registered notifier + gateway provisioner openwechat")


plugin = OpenWechatPlugin()

__all__ = ["OpenWechatNotifier", "OpenWechatPlugin", "plugin"]
