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
from mailflow.forms import FormField
from mailflow.notifications import DeliveryError, check_result, format_mail, targets_of
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

from .gateway import OpenWechatProvisioner

logger = logging.getLogger("mailflow.plugins.openwechat")


class OpenWechatNotifier:
    backend_id = "openwechat"

    def __init__(self, config: NotifierConfig) -> None:
        self._url = str(config.options.get("gateway_url", "")).rstrip("/")
        raw_targets: list[Any] = targets_of(config)
        self._targets: list[tuple[str, str]] = []
        for entry in raw_targets:
            text = str(entry).strip()
            kind, _, name = text.partition(":")
            kind = kind.strip().lower()
            kind = {"user": "contact", "group": "room"}.get(kind, kind)
            if kind in ("contact", "room") and name.strip():
                self._targets.append((kind, name.strip()))

    async def push_to_target(self, target: str, text: str) -> None:
        """Push one plain-text message to a single ``contact:<name>`` or
        ``room:<name>`` target (per-chat hourly-summary delivery)."""
        if not self._url:
            raise DeliveryError("configuration")
        kind, _, name = target.partition(":")
        kind = kind.strip().lower()
        kind = {"user": "contact", "group": "room"}.get(kind, kind)
        name = name.strip() or target.strip()
        if kind not in ("contact", "room"):
            kind, name = "contact", target.strip()
        payload = {
            "to": {"type": "room" if kind == "room" else "contact", "name": name},
            "text": text,
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(f"{self._url}/send", json=payload)
            response.raise_for_status()
            check_result(response.json())

    async def notify(self, record: MailRecord) -> None:
        await self.push_text(format_mail(record))

    async def notify_to_target(
        self, target: str, record: MailRecord, *, text: str | None = None
    ) -> None:
        await self.push_to_target(target, text if text is not None else format_mail(record))

    async def push_text(self, text: str) -> None:
        """Return only after every configured destination accepted the push."""
        if not self._url or not self._targets:
            raise DeliveryError("configuration")
        failure: Exception | None = None
        for kind, name in self._targets:
            try:
                await self.push_to_target(f"{kind}:{name}", text)
            except Exception as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure


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
        registrar.add_form_fields(
            ComponentKind.NOTIFIER,
            "openwechat",
            (
                FormField("gateway_url", required=True),
                FormField("admins", kind="list", description_key="tui.extras_admins"),
                FormField("targets", kind="list", description_key="tui.extras_targets"),
            ),
        )
        registrar.add_gateway_provisioner("openwechat", lambda: OpenWechatProvisioner())
        # component registration is routine startup detail, not something
        # the user needs at INFO — it fires on every app start whether or
        # not any instance is deployed
        logger.debug("registered notifier + gateway provisioner openwechat")


plugin = OpenWechatPlugin()

__all__ = ["OpenWechatNotifier", "OpenWechatPlugin", "plugin"]
