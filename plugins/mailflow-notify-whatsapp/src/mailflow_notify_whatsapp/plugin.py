"""MailFlow plugin: WhatsApp gateway (auto-deploy) based on Baileys.

Registers the ``whatsapp`` gateway provisioner (scan-to-login with no
platform token: the bridge installs from npm and shows a QR) and the
``whatsapp`` notifier, which posts to the bridge's ``POST /send`` endpoint.
See ``gateway.py`` for the provisioner contract and
``gateway/whatsapp-bridge.mjs`` for the bridge itself.

Options:
- ``gateway_url`` — bridge base URL (e.g. ``http://127.0.0.1:8898``),
  written by the guided setup
- ``gateway``     — ``whatsapp`` marks a MailFlow-managed instance
- ``targets``     — list of ``group:<jid>`` / ``user:<jid>`` entries
- ``admins``      — WhatsApp numbers allowed to run chat commands

Delivery failures propagate to the runtime for visible, targeted retries.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx
from mailflow.config import MailFlowConfig, NotifierConfig
from mailflow.domain import ComponentKind, MailRecord, NotificationImage
from mailflow.forms import FormField
from mailflow.message_media import ImageNotifier
from mailflow.notifications import DeliveryError, check_http_response, format_mail, targets_of
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

from .gateway import WhatsappProvisioner

logger = logging.getLogger("mailflow.notify.whatsapp")


def format_message(record: MailRecord) -> str:
    sender = record.mail.sender.display or record.mail.sender.address
    lines = [
        f"[MailFlow] {record.effective_urgency.value.upper()} — {record.mail.subject}",
        f"From: {sender}",
    ]
    summary = record.summary
    if summary:
        lines.append(summary[:500])
    attachments = [a.filename for a in record.mail.attachments if a.filename]
    if attachments:
        shown = ", ".join(attachments[:4])
        more = f" (+{len(attachments) - 4})" if len(attachments) > 4 else ""
        lines.append(f"Attachments: {shown}{more}")
    return "\n".join(lines)


class WhatsappNotifier(ImageNotifier):
    backend_id = "whatsapp"

    def __init__(self, config: NotifierConfig) -> None:
        self._url = str(config.options.get("gateway_url", "")).rstrip("/")
        raw_targets: list[Any] = targets_of(config)
        self._targets: list[tuple[str, str]] = []
        for entry in raw_targets:
            text = str(entry).strip()
            kind, _, name = text.partition(":")
            kind = kind.strip().lower()
            if kind not in ("user", "group") or not name.strip():
                logger.warning("whatsapp notifier: ignoring malformed target %r", text)
                continue
            self._targets.append((kind, name.strip()))

    @staticmethod
    def _payload(kind: str, name: str, text: str) -> dict[str, Any]:
        # the bridge appends @g.us / @s.whatsapp.net to a bare id, so the
        # stored target stays a plain jid-or-id either way
        return {
            "to": {"type": "group" if kind == "group" else "contact", "name": name},
            "text": text,
        }

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

    async def push_to_target(self, target: str, text: str) -> None:
        """Push one plain-text message to a single ``user:<jid>`` or
        ``group:<jid>`` target (per-chat hourly-summary delivery)."""
        if not self._url:
            raise DeliveryError("configuration")
        kind, _, name = target.partition(":")
        kind = kind.strip().lower()
        name = name.strip() or target.strip()
        if kind not in ("user", "group"):
            kind, name = "user", target.strip()
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(f"{self._url}/send", json=self._payload(kind, name, text))
            check_http_response(response)

    async def push_image_to_target(self, target: str, image: NotificationImage) -> None:
        if not self._url:
            raise DeliveryError("configuration")
        kind, _, name = target.partition(":")
        payload = {
            "to": {"type": "group" if kind == "group" else "contact", "name": name or target},
            "image": base64.b64encode(image.data).decode("ascii"),
            "content_type": image.content_type,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self._url}/send", json=payload)
            check_http_response(response)


class WhatsappPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PluginInfo(
            plugin_id="mailflow-notify-whatsapp",
            name="WhatsApp (Baileys scan-to-login)",
            version="0.1.0",
            description="WhatsApp gateway with QR scan-to-login, no platform token",
            kinds=[ComponentKind.NOTIFIER, ComponentKind.GATEWAY_PROVISIONER],
        )

    def mailflow_register(self, registrar: PluginRegistrar, config: MailFlowConfig) -> None:
        registrar.add_notifier("whatsapp", WhatsappNotifier)
        registrar.add_form_fields(
            ComponentKind.NOTIFIER,
            "whatsapp",
            (
                FormField("gateway_url", required=True),
                FormField("admins", kind="list", description_key="tui.extras_admins"),
                FormField("targets", kind="list", description_key="tui.extras_targets"),
            ),
        )
        registrar.add_gateway_provisioner("whatsapp", lambda: WhatsappProvisioner())
        # component registration is routine startup detail, not something
        # the user needs at INFO — it fires on every app start whether or
        # not any instance is deployed
        logger.debug("registered notifier + gateway provisioner whatsapp")


plugin = WhatsappPlugin()

__all__ = ["WhatsappNotifier", "WhatsappPlugin", "format_message", "plugin"]
