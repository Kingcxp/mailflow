"""Check declared connections without sending a message or starting a gateway."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from mailflow.contracts import MessageSource, ReplyPreflightSource
from mailflow.domain import ComponentKind
from mailflow.errors import error_text
from mailflow.notifications import connection_configured, targets_of
from mailflow.permissions import require_admin

if TYPE_CHECKING:
    from mailflow.service import MailFlowService


async def check_connections(
    service: MailFlowService, values: dict[str, Any]
) -> list[dict[str, str]]:
    require_admin(service.t)
    rows: list[dict[str, str]] = []

    async def probe(
        kind: ComponentKind, provider: str, options: dict[str, Any], settings: str, name: str
    ) -> None:
        operation = service.registry.probe(kind, provider)
        message = service.t("experience.connection_unverified")
        if operation:
            try:
                message = await asyncio.wait_for(operation(options, service.t), 25)
            except Exception as exc:
                message = error_text(exc, service.t)
        rows.append({"name": name, "message": message, "settings": settings})

    for index, account in enumerate(service.config.accounts):
        await probe(
            ComponentKind.MAIL_SOURCE,
            account.provider,
            account.options,
            f"accounts[{index}]",
            service.t("experience.receiving", name=account.account_id),
        )
        source = service.sources.get(account.account_id)
        sending = "experience.send_unavailable"
        if isinstance(source, ReplyPreflightSource):
            try:
                await source.check_reply_ready()
                sending = "experience.send_configured"
            except Exception as exc:
                rows.append(
                    {
                        "name": service.t("experience.sending", name=account.account_id),
                        "message": error_text(exc, service.t),
                        "settings": f"accounts[{index}]",
                    }
                )
                continue
        elif isinstance(source, MessageSource):
            sending = "experience.connection_unverified"
        rows.append(
            {
                "name": service.t("experience.sending", name=account.account_id),
                "message": service.t(sending),
                "settings": f"accounts[{index}]",
            }
        )
    for index, model in enumerate(service.config.llms):
        await probe(
            ComponentKind.LLM_BACKEND,
            model.provider,
            model.model_dump(),
            f"llms[{index}]",
            model.name or model.llm_id,
        )
    for index, channel in enumerate(service.config.notifiers):
        await probe(
            ComponentKind.NOTIFIER,
            channel.provider,
            channel.options,
            f"notifiers[{index}]",
            channel.notifier_id,
        )
        rows.append(
            {
                "name": channel.notifier_id,
                "message": service.t(
                    "experience.delivery_configured"
                    if channel.enabled and connection_configured(channel) and targets_of(channel)
                    else "experience.delivery_incomplete"
                ),
                "settings": f"notifiers[{index}]",
            }
        )
    return rows
