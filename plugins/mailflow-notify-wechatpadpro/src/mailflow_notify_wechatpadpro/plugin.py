"""WeChat notifier via WeChatPadPro (WeChat Pad protocol gateway).

Component id ``wechatpadpro``. Talks to a WeChatPadPro gateway that the
companion provisioner auto-deploys with docker compose (or any externally
hosted WeChatPadPro instance).

Options:
- ``base_url``   — gateway API root (e.g. ``http://127.0.0.1:8100``)
- ``auth_key``   — per-device auth key (授权码) for an externally hosted
  gateway; when empty the notifier can mint one from ``admin_key``.
- ``admin_key``  — external gateway ADMIN_KEY used only to mint an auth key.
- ``gateway``    — ``wechatpadpro`` marks a MailFlow-managed instance. Its
  minted auth key stays in the per-instance state and is loaded automatically;
  it is never copied into the user-editable notifier configuration.
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
from mailflow.notifications import DeliveryError, check_result, format_mail, targets_of
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

from .gateway import managed_notifier_auth_key

logger = logging.getLogger("mailflow.notify.wechatpadpro")


class WechatPadProNotifier:
    backend_id = "wechatpadpro"

    def __init__(self, config: NotifierConfig) -> None:
        self._url = str(config.options.get("base_url", "")).rstrip("/")
        self._auth_key = str(config.options.get("auth_key", ""))
        self._admin_key = str(config.options.get("admin_key", ""))
        if not self._auth_key and config.options.get("gateway") == "wechatpadpro":
            self._auth_key = managed_notifier_auth_key(config.notifier_id)
        raw_targets: list[Any] = targets_of(config)
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
        response.raise_for_status()
        raw: Any = response.json()
        check_result(cast_dict(raw))
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
        """Push one plain-text message to a single ``user:<wxid>`` or
        ``group:<chatroom>`` target (per-chat hourly-summary delivery)."""
        if not self._url:
            raise DeliveryError("configuration")
        _kind, _, wxid = target.partition(":")
        wxid = wxid.strip() or target.strip()
        if not wxid:
            raise DeliveryError("configuration")
        async with httpx.AsyncClient(timeout=20.0) as client:
            key = await self._key(client)
            if not key:
                raise DeliveryError("configuration")
            response = await client.post(
                f"{self._url}/Msg/SendTxt",
                params={"key": key},
                json={"Wxid": "", "ToWxid": wxid, "Content": text, "Type": 0},
            )
            response.raise_for_status()
            check_result(response.json())

    async def notify(self, record: MailRecord) -> None:
        await self.push_text(format_mail(record))

    async def notify_to_target(
        self, target: str, record: MailRecord, *, text: str | None = None
    ) -> None:
        await self.push_to_target(target, text if text is not None else format_mail(record))


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
