"""OneBot v11 notifier (NapCat / go-cqhttp): pushes mail alerts to QQ
users and groups over the standard HTTP API.

Component id ``onebot``. Options:
- ``http_url``   — OneBot HTTP server root (e.g. ``http://127.0.0.1:3000``)
- ``access_token`` — shared secret sent as ``Authorization: Bearer …``
- ``targets``    — list of ``user:<qq>`` / ``group:<group_id>`` entries

Missing configuration skips delivery gracefully (the same contract as
the other notifiers); transport failures are logged, never raised.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from mailflow.config import NotifierConfig
from mailflow.domain import ComponentKind, MailRecord
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

logger = logging.getLogger("mailflow.notify.onebot")


class OneBotNotifier:
    backend_id = "onebot"

    def __init__(self, config: NotifierConfig) -> None:
        self._url = str(config.options.get("http_url", "")).rstrip("/")
        self._token = str(config.options.get("access_token", ""))
        raw_targets: list[Any] = list(config.options.get("targets") or [])
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

    async def notify(self, record: MailRecord) -> None:
        if not self._url or not self._targets:
            logger.warning(
                "onebot notifier: http_url/targets not configured; skipping (record %s)",
                record.record_id,
            )
            return
        text = format_message(record)
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            for kind, identifier in self._targets:
                endpoint = (
                    f"{self._url}/send_private_msg"
                    if kind == "user"
                    else f"{self._url}/send_group_msg"
                )
                payload = (
                    {"user_id": int(identifier), "message": text}
                    if kind == "user"
                    else {"group_id": int(identifier), "message": text}
                )
                try:
                    response = await client.post(endpoint, json=payload, headers=headers)
                    response.raise_for_status()
                except Exception as exc:
                    logger.warning("onebot delivery to %s:%s failed: %s", kind, identifier, exc)


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
    kinds=[ComponentKind.NOTIFIER],
)


class NotifierPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        registrar.add_notifier("onebot", OneBotNotifier)


plugin = NotifierPlugin()

__all__ = ["NotifierPlugin", "OneBotNotifier", "plugin"]
