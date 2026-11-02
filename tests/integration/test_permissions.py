"""Identity scopes protect shared commands and direct service operations."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from mailflow.commands import CommandRouter
from mailflow.config import NotifierConfig
from mailflow.permissions import AccessDenied, PermissionContext, current_context, permission_scope
from mailflow_server.app import create_app

from mailflow_test_support.commands import commands_service, make_record

VIEWER = PermissionContext("reader", "telegram", "bot-1", "chat-1")


@pytest.mark.parametrize("language", ["en", "zh-CN"])
async def test_shared_router_allows_reads_and_denies_writes(language: str) -> None:
    service = commands_service()
    service.i18n.set_language(language)
    router = CommandRouter(service)
    for line in ("help", "mail list", "action list", "runtime"):
        assert (await router.execute(line, context=VIEWER)).ok
    for line in (
        "mail delete missing",
        "mail urgency missing urgent",
        "mail wipe CONFIRM",
        "action add changed",
        "action edit missing",
        "action delete missing",
        "reply create missing",
        "reply confirm missing token",
        "config set general.workers 2",
        "plugin install missing",
        "plugin uninstall missing",
        "plugin enable missing",
        "lang set en",
        "update now",
        "update auto on",
        "clean records",
        "feedback missing changed",
    ):
        response = await router.execute(line, context=VIEWER)
        assert not response.ok, line
        assert response.text == service.t("permission.denied"), line
    assert current_context().provider == "local"
    assert (await router.execute("help")).ok
    service.list_mails = AsyncMock(side_effect=NotImplementedError("provider detail"))
    assert (await router.execute("mail list", context=VIEWER)).text == service.t(
        "common.unsupported"
    )


async def test_direct_mutations_cannot_escape_current_identity() -> None:
    service = commands_service()
    with permission_scope(VIEWER):
        for operation in (
            service.delete_mail("missing"),
            service.delete_action("missing"),
            service.set_setting("general.workers", 2),
            service.confirm_reply("missing", "token"),
            service.gateway_shutdown("telegram", "bot-1"),
            service.apply_updates(),
        ):
            with pytest.raises(AccessDenied):
                await operation


def test_admin_grants_are_scoped_to_enabled_provider_and_instance() -> None:
    service = commands_service()
    service.config.notifiers = [
        NotifierConfig(notifier_id="bot-1", provider="onebot", options={"admins": ["42"]}),
        NotifierConfig(notifier_id="bot-2", provider="onebot", options={"admins": ["99"]}),
        NotifierConfig(
            notifier_id="disabled", provider="telegram", enabled=False, options={"admins": ["42"]}
        ),
    ]
    assert service.permission_context(sender="42", provider="napcat", instance_id="bot-1").is_admin
    for provider, instance in (
        ("onebot", "bot-2"),
        ("onebot", ""),
        ("telegram", "disabled"),
        ("", ""),
    ):
        assert not service.permission_context(
            sender="42", provider=provider, instance_id=instance
        ).is_admin


async def test_concurrent_contexts_remain_isolated() -> None:
    async def inspect(context: PermissionContext) -> PermissionContext:
        with permission_scope(context):
            await asyncio.sleep(0)
            return current_context()

    admin = PermissionContext("admin", "local", is_admin=True)
    assert await asyncio.gather(inspect(VIEWER), inspect(admin)) == [VIEWER, admin]


@pytest.mark.parametrize("language", ["en", "zh-CN"])
async def test_missing_account_and_history_capability_are_translated(
    language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = commands_service()
    service.i18n.set_language(language)
    with pytest.raises(KeyError) as missing:
        await service.fetch_history("missing")
    assert missing.value.args[0] == service.t("account.not_found", account_id="missing")
    monkeypatch.setitem(service.sources, "read-only", cast(Any, object()))
    with pytest.raises(NotImplementedError) as unsupported:
        await service.fetch_history("read-only")
    assert str(unsupported.value) == service.t(
        "account.history_unsupported", account_id="read-only"
    )


@pytest.mark.parametrize(
    "provider", ["napcat", "telegram", "openwechat", "wechatpadpro", "whatsapp"]
)
async def test_chat_transports_keep_sender_and_instance_through_shared_router(
    provider: str,
) -> None:
    service = commands_service()
    configured_provider = "onebot" if provider == "napcat" else provider
    service.config.notifiers = [
        NotifierConfig(
            notifier_id="bot-1",
            provider=configured_provider,
            options={"admins": ["42"]},
        )
    ]
    CommandRouter(service)
    await service.storage.save_mail(make_record())
    observed: list[PermissionContext] = []

    async def remove(record_id: str) -> bool:
        observed.append(current_context())
        return True

    service.delete_mail = AsyncMock(side_effect=remove)
    for sender in ("reader", "42"):
        reply = await service.command_dispatch(
            "/mailflow mail delete m1",
            sender=sender,
            provider=provider,
            instance_id="bot-1",
            chat_id="chat-1",
        )
        assert reply
        if sender == "reader":
            assert service.t("permission.denied") in reply
    assert observed == [PermissionContext("42", provider, "bot-1", "chat-1", True)]


async def test_explicit_local_chat_context_retains_local_trust() -> None:
    service = commands_service()
    CommandRouter(service)
    await service.storage.save_mail(make_record())
    service.delete_mail = AsyncMock(return_value=True)
    context = PermissionContext("local", "local", chat_id="local-chat", is_admin=True)
    assert await service.command_dispatch("/mailflow mail delete m1", context=context)
    service.delete_mail.assert_awaited_once_with("m1")


async def test_rest_uses_shared_permissions_and_ignores_body_role() -> None:
    service = commands_service()
    service.config.server.username, service.config.server.password = "admin", "secret"
    app = create_app(service)
    app.dependency_overrides[app.state.authenticate] = lambda: VIEWER
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fake"
    ) as client:
        assert (await client.get("/mails")).status_code == 200
        assert (await client.get("/snapshot")).status_code == 200
        for method, path, body in (
            ("DELETE", "/mails/missing", {}),
            ("POST", "/mails/missing/urgency", {"urgency": "urgent"}),
            ("PUT", "/settings/general.workers", {"value": 2}),
            ("POST", "/plugins/anything/enable", {}),
            ("POST", "/drafts/missing/confirm", {"token": "x"}),
        ):
            response = await client.request(method, path, json={**body, "is_admin": True})
            assert response.status_code == 403, response.text
            assert response.json()["detail"]["code"] == "permission.denied"
        response = await client.post(
            "/commands", json={"line": "mail delete missing", "is_admin": True}
        )
        assert response.json() == {"ok": False, "text": service.t("permission.denied")}
        assert (await client.get("/settings/sections")).status_code == 403
        for method, path, body in (
            ("POST", "/actions", {"summary": "changed", "due_at": "2026-12-01T10:00:00Z"}),
            ("PUT", "/actions/missing", {"summary": "changed"}),
            ("DELETE", "/actions/missing", {}),
            ("POST", "/plugins/anything/uninstall", {}),
        ):
            response = await client.request(method, path, json=body)
            assert response.status_code == 403, response.text


def test_websocket_auth_and_log_permission_use_shared_identity() -> None:
    service = commands_service()
    service.config.server.username, service.config.server.password = "admin", "secret"
    app = create_app(service)
    app.dependency_overrides[app.state.authenticate] = lambda: VIEWER
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "logs", "is_admin": True})
        frame = ws.receive_json()
        assert frame["event"] == "error"
        assert frame["payload"]["code"] == "permission.denied"
