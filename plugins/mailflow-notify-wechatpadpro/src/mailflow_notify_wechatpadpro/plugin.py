"""WeChat notifier via WeChatPadPro (WeChat Pad protocol gateway).

Component id ``wechatpadpro``. Talks to a WeChatPadPro gateway that the
companion provisioner auto-deploys with docker compose (or any externally
hosted WeChatPadPro instance).

Options:
- ``base_url``   — gateway API root (e.g. ``http://127.0.0.1:8100``)
- ``auth_key``   — per-device auth key (授权码); when empty the notifier
  asks the gateway to mint one from its ``admin_key`` option
- ``admin_key``  — gateway ADMIN_KEY (used only to mint an auth key)
- ``targets``    — list of ``user:<wxid>`` / ``group:<chatroom_id>``
  entries; a bare ``wxid`` is treated as ``user:``

Send contract: ``POST {base_url}/Msg/SendTxt`` with
``{"Wxid": <bot wxid or "">, "ToWxid": <target>, "Content": <text>,
"Type": 0}``; success is ``Code == 0`` in the response JSON. Transport
failures are logged, never raised.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import httpx
from mailflow.config import NotifierConfig
from mailflow.domain import ComponentKind, MailRecord
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

logger = logging.getLogger("mailflow.notify.wechatpadpro")


class WechatPadProNotifier:
    backend_id = "wechatpadpro"

    def __init__(self, config: NotifierConfig) -> None:
        self._url = str(config.options.get("base_url", "")).rstrip("/")
        self._auth_key = str(config.options.get("auth_key", ""))
        self._admin_key = str(config.options.get("admin_key", ""))
        raw_targets: list[Any] = list(config.options.get("targets") or [])
        self._targets: list[tuple[str, str]] = []
        for entry in raw_targets:
            text = str(entry).strip()
            kind, _, identifier = text.partition(":")
            kind = kind.strip().lower()
            if kind not in ("user", "group"):
                # bare wxid: treat as a direct contact
                kind, identifier = "user", text
            if not identifier.strip():
                logger.warning("wechatpadpro notifier: ignoring malformed target %r", text)
                continue
            self._targets.append((kind, identifier.strip()))

    async def _key(self, client: httpx.AsyncClient) -> str:
        """The auth key: configured, or minted once from the admin key."""
        if self._auth_key:
            return self._auth_key
        if not self._admin_key:
            return ""
        response = await client.post(
            f"{self._url}/admin/GenAuthKey1",
            params={"key": self._admin_key},
            json={"Count": 1, "Days": 3650},
        )
        raw: Any = response.json()
        payload: dict[str, Any] = dict(cast_dict(raw))
        data: Any = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            first = cast_dict(data[0])
            self._auth_key = str(first.get("Key") or "")
        elif isinstance(data, dict):
            mapped = cast_dict(data)
            self._auth_key = str(mapped.get("Key") or mapped.get("key") or "")
        if not self._auth_key:
            logger.warning(
                "wechatpadpro notifier: could not mint an auth key from the "
                "admin key; set options.auth_key directly"
            )
        return self._auth_key

    async def _send(self, client: httpx.AsyncClient, to_wxid: str, text: str) -> None:
        key = await self._key(client)
        if not key:
            return
        response = await client.post(
            f"{self._url}/Msg/SendTxt",
            params={"key": key},
            json={"Wxid": "", "ToWxid": to_wxid, "Content": text, "Type": 0},
        )
        try:
            payload: Any = response.json()
        except Exception:
            payload = {}
        if response.status_code >= 400 or payload.get("Code") not in (0, "0", None):
            logger.warning(
                "wechatpadpro notifier: send to %s rejected: HTTP %d %s",
                to_wxid,
                response.status_code,
                str(payload)[:120],
            )

    async def push_text(self, text: str) -> None:
        """Push a plain-text message (schedule reminders, daily digest)."""
        if not self._url or not self._targets:
            return
        async with httpx.AsyncClient(timeout=20.0) as client:
            for _kind, wxid in self._targets:
                try:
                    await self._send(client, wxid, text)
                except Exception as exc:
                    logger.warning("wechatpadpro text push to %s failed: %s", wxid, exc)

    async def push_to_target(self, target: str, text: str) -> None:
        """Push one plain-text message to a single ``user:<wxid>`` or
        ``group:<chatroom>`` target (per-chat hourly-summary delivery)."""
        _kind, _, wxid = target.partition(":")
        wxid = wxid.strip() or target.strip()
        if not wxid:
            return
        async with httpx.AsyncClient(timeout=20.0) as client:
            key = await self._key(client)
            if not key:
                return
            response = await client.post(
                f"{self._url}/Msg/SendTxt",
                params={"key": key},
                json={"Wxid": "", "ToWxid": wxid, "Content": text, "Type": 0},
            )
            if response.status_code >= 400:
                logger.warning(
                    "wechatpadpro notifier: targeted push to %s rejected: HTTP %d",
                    wxid,
                    response.status_code,
                )

    async def notify(self, record: MailRecord) -> None:
        if not self._url or not self._targets:
            logger.warning(
                "wechatpadpro notifier: base_url/targets not configured; skipping (record %s)",
                record.record_id,
            )
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
            for _kind, wxid in self._targets:
                try:
                    await self._send(client, wxid, text)
                except Exception as exc:
                    logger.warning("wechatpadpro delivery to %s failed: %s", wxid, exc)


def format_message(record: MailRecord) -> str:  # kept for parity with other notifiers
    sender = record.mail.sender.display or record.mail.sender.address
    return f"[MailFlow] {record.effective_urgency.value.upper()} — {record.mail.subject}\nFrom: {sender}"


def cast_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        typed: dict[str, Any] = cast("dict[str, Any]", value)
        return dict(typed)
    return {}


PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-notify-wechatpadpro",
    name="WeChat Notifier (WeChatPadPro)",
    version="0.1.0",
    description=(
        "Pushes mail alerts via a WeChatPadPro gateway (WeChat Pad protocol) "
        "with docker compose auto-deploy, QR login in the TUI and chat "
        "commands over a webhook bridge"
    ),
    kinds=[ComponentKind.NOTIFIER, ComponentKind.GATEWAY_PROVISIONER],
)


class NotifierPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        from .gateway import WechatPadProProvisioner

        registrar.add_notifier("wechatpadpro", WechatPadProNotifier)
        registrar.add_gateway_provisioner("wechatpadpro", lambda: WechatPadProProvisioner())


plugin = NotifierPlugin()

__all__ = ["NotifierPlugin", "WechatPadProNotifier", "plugin"]
