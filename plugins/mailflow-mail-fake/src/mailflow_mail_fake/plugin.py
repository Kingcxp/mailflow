"""Fake mail source plugin: deterministic local mails for dev/demo setups.

Mails are declared in the account options as a ``mails`` list of dicts
(``message_id``, ``subject``, ``sender``, ``sender_name``, ``body``,
``body_html``, ``urgency``). Timestamps are deterministic: each message gets
a fixed base date plus its index in minutes, unless overridden by an option.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from mailflow.config import MailAccountConfig
from mailflow.domain import ComponentKind, MailAddress, MailMessage
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar
from mailflow_testkit.fakes import FakeMailSource

_BASE = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)

PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-mail-fake",
    name="Fake Mail Source",
    version="0.1.0",
    description="Deterministic local mail source for development and demos",
    kinds=[ComponentKind.MAIL_SOURCE],
)


def _parse_mail(account_id: str, index: int, raw: dict[str, Any], base: datetime) -> MailMessage:
    sender = MailAddress(
        name=str(raw.get("sender_name", "")),
        address=str(raw.get("sender", "sender@example.com")),
    )
    timestamp = base + timedelta(minutes=index)
    message_id = str(raw.get("message_id") or f"fake-{index}")
    urgency_value = raw.get("urgency")
    return MailMessage(
        message_id=message_id,
        account_id=account_id,
        subject=str(raw.get("subject", "(no subject)")),
        sender=sender,
        recipients=[MailAddress(address=account_id)],
        cc=[],
        date=timestamp,
        received_at=timestamp,
        body_text=str(raw.get("body", "")),
        body_html=str(raw.get("body_html", "")),
        provider="mailflow-mail-fake",
        provider_message_id=message_id,
        headers={"X-Fake-Mail": str(urgency_value or "")} if urgency_value else {},
    )


def build_source(account: MailAccountConfig) -> FakeMailSource:
    options = account.options
    base = _BASE
    base_value = options.get("base_time")
    if base_value:
        try:
            base = datetime.fromisoformat(str(base_value)).astimezone(UTC)
        except ValueError:
            base = _BASE
    mails = [
        _parse_mail(account.account_id, i, raw, base)
        for i, raw in enumerate(options.get("mails", []))
    ]
    return FakeMailSource(
        mails,
        fail=bool(options.get("fail", False)),
        delay=float(options.get("delay", 0.0)),
    )


class FakeMailPlugin:
    @property
    def info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        registrar.add_source("fake", build_source)


plugin = FakeMailPlugin()


__all__ = ["FakeMailPlugin", "build_source", "plugin"]
