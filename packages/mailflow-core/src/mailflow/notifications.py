"""Shared notification targets, readable messages and delivery failures."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from typing import Any, cast
from urllib.request import Request

from mailflow.config import NotifierConfig
from mailflow.domain import MailRecord
from mailflow.i18n import I18n


class DeliveryError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.code = f"delivery.{reason}"
        self.params: dict[str, Any] = {}


def option_enabled(options: dict[str, Any], key: str, default: bool = True) -> bool:
    value = options.get(key, default)
    if isinstance(value, str):
        return value.strip().casefold() not in {"", "0", "false", "off", "no"}
    return bool(value)


def target_id(target: str) -> str:
    return (target.partition(":")[2] or target).strip()


def targets_of(config: NotifierConfig) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()
    raw = [
        *config.options.get("targets", []),
        *config.options.get("subscription_targets", []),
        config.options.get("chat_id", ""),
    ]
    if config.provider == "smtp":
        raw.extend(config.options.get("to", []))
    for value in raw:
        target = str(value or "").strip()
        identity = target_id(target)
        # OneBot private and group numeric IDs belong to different namespaces.
        if config.provider == "onebot":
            identity = target
        if target and identity not in seen:
            excluded = [str(value) for value in config.options.get("excluded_targets", [])]
            if any(
                (
                    value == target
                    if config.provider == "onebot"
                    else target_id(value) == target_id(target)
                )
                for value in excluded
            ):
                continue
            targets.append(target)
            seen.add(identity)
    return targets


def connection_configured(config: NotifierConfig) -> bool:
    required = {
        "onebot": "http_url",
        "telegram": "bot_token",
        "openwechat": "gateway_url",
        "whatsapp": "gateway_url",
        "wechatpadpro": "base_url",
    }
    name = required.get(config.provider)
    if name is None or not config.options.get(name):
        return False
    return config.provider != "wechatpadpro" or bool(
        config.options.get("auth_key")
        or config.options.get("admin_key")
        or config.options.get("gateway")
    )


def format_mail(record: MailRecord, translate: Callable[..., str] | None = None) -> str:
    t = translate or I18n().t
    lines = [
        t(
            "delivery.mail_title",
            urgency=t(f"urgency.{record.effective_urgency.value}"),
            subject=record.mail.subject,
        ),
        t("delivery.sender", sender=record.mail.sender.display),
        t("delivery.mail_ref", id=record.record_id),
    ]
    if record.summary:
        lines.append(record.summary[:1000])
    attachments = [a.filename for a in record.mail.attachments if a.filename]
    if attachments:
        lines.append(t("delivery.attachments", names=", ".join(attachments[:4])))
    return "\n".join(lines)


def check_result(payload: Any) -> None:
    """Check the explicit rejection fields used by bundled adapters."""
    if not isinstance(payload, dict) or not payload:
        raise DeliveryError("rejected")
    payload = cast("dict[str, Any]", payload)
    if (
        payload.get("ok") is False
        or payload.get("Success") is False
        or payload.get("status") == "failed"
        or any(
            payload.get(key, 0) not in (0, "0", None)
            for key in ("retcode", "errcode", "code", "Code", "StatusCode")
        )
    ):
        raise DeliveryError("rejected")


def post_webhook(request: Request, timeout: float, *, provider: str = "") -> None:
    """Consume and close responses; an HTTP success can still reject a message."""
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    if not body.strip():
        if provider in {"feishu", "dingtalk", "wecom", "serverchan"}:
            raise DeliveryError("rejected")
        return
    try:
        result = json.loads(body)
    except ValueError:
        if provider == "slack" and body.strip() != "ok":
            raise DeliveryError("rejected") from None
        if provider in {"feishu", "dingtalk", "wecom", "serverchan"}:
            raise DeliveryError("rejected") from None
        return
    if isinstance(result, dict):
        check_result(cast("dict[str, Any]", result))
