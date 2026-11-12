"""OneBot v11 notifier (NapCat / go-cqhttp): pushes mail alerts to QQ
users and groups over the standard HTTP API.

Component id ``onebot``. Options:
- ``http_url``   — OneBot HTTP server root (e.g. ``http://127.0.0.1:3000``)
- ``access_token`` — shared secret sent as ``Authorization: Bearer …``
- ``targets``    — list of ``user:<qq>`` / ``group:<group_id>`` entries

Delivery failures propagate to the runtime for visible, targeted retries.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx
from mailflow.config import NotifierConfig
from mailflow.domain import ComponentKind, MailRecord, NotificationCapabilities, NotificationImage
from mailflow.forms import FormField
from mailflow.message_media import ImageNotifier
from mailflow.notifications import DeliveryError, check_http_response, format_mail, targets_of
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

logger = logging.getLogger("mailflow.notify.onebot")


class OneBotNotifier(ImageNotifier):
    backend_id = "onebot"
    capabilities = NotificationCapabilities(images=True, files=True)

    def __init__(self, config: NotifierConfig) -> None:
        self._url = str(config.options.get("http_url", "")).rstrip("/")
        self._token = str(config.options.get("access_token", ""))
        raw_targets: list[Any] = targets_of(config)
        self._targets: list[tuple[str, str]] = []
        for entry in raw_targets:
            text = str(entry).strip()
            kind, _, identifier = text.partition(":")
            kind = kind.strip().lower()
            if kind not in ("user", "group") or not identifier.strip():
                logger.warning("onebot notifier: ignoring malformed target %r", text)
                continue
            # the send payload needs a numeric id: reject garbage here so a
            # single bad line cannot abort delivery to the remaining targets
            try:
                int(identifier.strip())
            except ValueError:
                logger.warning("onebot notifier: target %r has a non-numeric id; ignoring", text)
                continue
            self._targets.append((kind, identifier.strip()))

    async def push_text(self, text: str) -> None:
        """Return only after every configured destination accepted the push."""
        if not self._url or not self._targets:
            raise DeliveryError("configuration")
        failure: Exception | None = None
        for kind, identifier in self._targets:
            try:
                await self.push_to_target(f"{kind}:{identifier}", text)
            except Exception as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure

    async def push_to_target(self, target: str, text: str) -> None:
        await self._send(target, text)

    async def push_file_to_target(self, target: str, file: NotificationImage) -> None:
        await self._send(
            target,
            [
                {
                    "type": "file",
                    "data": {
                        "file": "base64://" + base64.b64encode(file.data).decode("ascii"),
                        "name": file.filename,
                    },
                }
            ],
        )

    async def push_image_to_target(self, target: str, image: NotificationImage) -> None:
        data = base64.b64encode(image.data).decode("ascii")
        await self._send(target, [{"type": "image", "data": {"file": "base64://" + data}}])

    async def _send(self, target: str, text: str | list[dict[str, Any]]) -> None:
        """Push one plain-text message to a single ``user:<id>`` or
        ``group:<id>`` target (per-chat hourly-summary delivery)."""
        if not self._url:
            raise DeliveryError("configuration")
        kind, _, identifier = target.partition(":")
        kind = kind.strip().lower()
        identifier = identifier.strip() or target.strip()
        if kind not in ("user", "group"):
            kind, identifier = "user", target.strip()
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        endpoint = (
            f"{self._url}/send_private_msg" if kind == "user" else f"{self._url}/send_group_msg"
        )
        payload = (
            {"user_id": int(identifier), "message": text}
            if kind == "user"
            else {"group_id": int(identifier), "message": text}
        )
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
            check_http_response(resp)

    async def notify(self, record: MailRecord) -> None:
        await self.push_text(format_mail(record))

    async def notify_to_target(
        self, target: str, record: MailRecord, *, text: str | None = None
    ) -> None:
        await self.push_to_target(target, text if text is not None else format_mail(record))


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


PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-notify-onebot",
    name="OneBot (QQ) Notifier",
    version="0.1.0",
    description=(
        "Pushes mail alerts to QQ users/groups via an OneBot v11 HTTP server (NapCat, go-cqhttp)"
    ),
    kinds=[ComponentKind.NOTIFIER, ComponentKind.GATEWAY_PROVISIONER],
)


class NotifierPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        registrar.add_notifier("onebot", OneBotNotifier)
        registrar.add_form_fields(
            ComponentKind.NOTIFIER,
            "onebot",
            (
                FormField("http_url", required=True),
                FormField("access_token", kind="password", secret=True),
                FormField("admins", kind="list", description_key="tui.extras_admins"),
                FormField("targets", kind="list", description_key="tui.extras_targets"),
            ),
        )
        from mailflow_notify_onebot.gateway import NapCatProvisioner

        registrar.add_gateway_provisioner("napcat", NapCatProvisioner)


plugin = NotifierPlugin()

__all__ = ["NotifierPlugin", "OneBotNotifier", "plugin"]
