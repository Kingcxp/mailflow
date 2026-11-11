"""Telegram gateway provisioner + notifier tests.

Telegram has no local runtime to install — the Bot API *is* the gateway — so
what these tests pin is the part that can silently break: token validation,
the in-process long-poll bridge that carries chat commands, and the notifier's
credential errors and delivery to each configured target.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, ClassVar

import pytest
from mailflow.config import NotifierConfig
from mailflow.domain import MailRecord, Urgency
from mailflow_notify_telegram.gateway import (
    TelegramBridge,
    TelegramProvisioner,
)
from mailflow_notify_telegram.plugin import TelegramNotifier


def _record() -> MailRecord:
    from mailflow_testkit.fakes import make_mail

    return MailRecord(
        record_id="r1",
        mail=make_mail(message_id="r1", subject="Exam tomorrow"),
        auto_urgency=Urgency.IMPORTANT,
        analysis=None,
    )


def _notifier(**options: Any) -> TelegramNotifier:
    return TelegramNotifier(
        NotifierConfig(notifier_id="tg", provider="telegram", options=dict(options))
    )


class _StubResponse:
    def __init__(self, payload: Any) -> None:
        self.status_code = 200
        self._payload = payload

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        pass

    async def aiter_lines(self) -> AsyncIterator[str]:
        yield json.dumps(self._payload)


class _StubClient:
    """Minimal ``httpx.AsyncClient`` stand-in for the provisioner tests."""

    payload: ClassVar[Any] = {"ok": True}
    posts: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, **kwargs: Any) -> None:
        del kwargs

    async def __aenter__(self) -> _StubClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, url: str) -> _StubResponse:
        del url
        return _StubResponse(self.payload)

    @asynccontextmanager
    async def stream(
        self, method: str, url: str, json: Any = None
    ) -> AsyncGenerator[_StubResponse]:
        yield await self.post(url, json=json)

    async def post(self, url: str, json: Any = None) -> _StubResponse:
        _StubClient.posts.append({"url": url, **(json or {})})
        return _StubResponse(self.payload)


def _client_factory(**kwargs: Any) -> _StubClient:
    """Typed stand-in used with ``monkeypatch.setattr(httpx, "AsyncClient", ...)``."""
    return _StubClient(**kwargs)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """Capture send_text calls instead of hitting the Bot API."""
    calls: list[tuple[str, str, str]] = []
    plugin_module = importlib.import_module("mailflow_notify_telegram.plugin")

    def fake_send(token: str, chat_id: str, text: str) -> None:
        calls.append((token, chat_id, text))

    monkeypatch.setattr(plugin_module, "send_text", fake_send)
    return calls


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the provisioner's Bot API caller; records method calls."""
    state: dict[str, Any] = {"calls": [], "fail": None, "me": {"id": 7, "username": "mf_bot"}}
    import mailflow_notify_telegram.gateway as gateway_module

    async def fake_call(token: str, method: str, payload: dict[str, Any] | None = None) -> Any:
        state["calls"].append((method, payload or {}))
        if state["fail"] is not None:
            raise RuntimeError(str(state["fail"]))
        if method == "getMe":
            return {"ok": True, "result": dict(state["me"])}
        return {"ok": True, "result": []}

    monkeypatch.setattr(gateway_module, "_call", fake_call)
    return state


async def test_setup_checks_token_then_starts_reports_identity_and_stops(
    api: dict[str, Any],
) -> None:
    prov = TelegramProvisioner()
    assert (await prov.status("tg-1")).status == "stopped"
    assert (await prov.qr("tg-1")).startswith("ERROR:")
    with pytest.raises(RuntimeError):
        await prov.install("tg-1", {})
    with pytest.raises(RuntimeError):
        await prov.start("tg-1", {})
    assert api["calls"] == []
    api["fail"] = "telegram getMe: Unauthorized"
    with pytest.raises(RuntimeError, match="Unauthorized"):
        await prov.install("tg-1", {"bot_token": "bad"})
    assert (await prov.status("tg-1")).status == "stopped"
    api["fail"] = None
    await prov.install("tg-1", {"bot_token": "123:abc"})
    instance = await prov.start("tg-1", {"bot_token": "123:abc"})
    try:
        assert instance.status == "running" and instance.extra["username"] == "mf_bot"
        assert (await prov.status("tg-1")).status == "running"
        assert await prov.qr("tg-1") == "__MAILFLOW_LOGGED_IN__"
    finally:
        await prov.stop("tg-1")
    assert (await prov.status("tg-1")).status == "stopped"
    assert (await prov.qr("tg-1")).startswith("ERROR:")


async def test_changing_token_or_host_replaces_old_poller(api: dict[str, Any]) -> None:
    provisioner = TelegramProvisioner()
    first = await provisioner.ensure_bridge("one", {"bot_token": "old", "bot_url": "http://old"})
    first_task = first._task
    same = await asyncio.gather(
        *(
            provisioner.ensure_bridge("one", {"bot_token": "old", "bot_url": "http://old"})
            for _ in range(3)
        )
    )
    assert all(bridge is first for bridge in same)
    second = await provisioner.ensure_bridge("one", {"bot_token": "old", "bot_url": "http://new"})
    assert second is not first and first_task.done()
    second_task = second._task
    third = await provisioner.ensure_bridge("one", {"bot_token": "new", "bot_url": "http://new"})
    assert third is not second and second_task.done()
    await provisioner.stop("one")
    await provisioner.stop("one")
    assert not third.running


@pytest.mark.asyncio
async def test_detect_reports_api_reachability(monkeypatch: pytest.MonkeyPatch) -> None:
    import mailflow_notify_telegram.gateway as gateway_module

    _StubClient.payload = {"ok": True}
    monkeypatch.setattr(gateway_module.httpx, "AsyncClient", _client_factory)
    prov = TelegramProvisioner()
    assert "reachable" in await prov.detect()


@pytest.mark.asyncio
async def test_dispatch_forwards_the_command_payload(
    monkeypatch: pytest.MonkeyPatch, api: dict[str, Any]
) -> None:
    """The bridge must post the exact payload bot_server reads, and send
    back every reply page it returns."""
    import mailflow_notify_telegram.gateway as gateway_module

    _StubClient.posts = []
    _StubClient.payload = {"reply": ["page one", "page two"]}
    monkeypatch.setattr(gateway_module.httpx, "AsyncClient", _client_factory)
    bridge = TelegramBridge("tg-1", "123:abc", "http://127.0.0.1:18789/bot/message")
    await bridge._handle_update(  # pyright: ignore[reportPrivateUsage]
        {
            "update_id": 5,
            "message": {
                "text": "/mailflow help",
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 9},
            },
        }
    )
    assert _StubClient.posts == [
        {
            "url": "http://127.0.0.1:18789/bot/message",
            "text": "/mailflow help",
            "sender": "9",
            "chat_id": "42",
            "chat_type": "private",
            "provider": "telegram",
            "instance_id": "tg-1",
            "request_id": "5",
            "mentioned": False,
            "stream": True,
        }
    ]
    # both reply pages went back through sendMessage, in order
    sends = [call for call in api["calls"] if call[0] == "sendMessage"]
    assert [call[1]["text"] for call in sends] == ["page one", "page two"]


@pytest.mark.asyncio
async def test_dispatch_ignores_self_and_non_text_messages(
    monkeypatch: pytest.MonkeyPatch, api: dict[str, Any]
) -> None:
    import mailflow_notify_telegram.gateway as gateway_module

    _StubClient.posts = []
    _StubClient.payload = {"reply": "must never be dispatched"}
    monkeypatch.setattr(gateway_module.httpx, "AsyncClient", _client_factory)
    bridge = TelegramBridge("tg-1", "123:abc", "http://x/bot/message")
    await bridge._load_identity()  # pyright: ignore[reportPrivateUsage]
    # the bot's own message (id 7 from getMe)
    await bridge._handle_update(  # pyright: ignore[reportPrivateUsage]
        {"update_id": 1, "message": {"text": "hi", "chat": {"id": 1}, "from": {"id": 7}}}
    )
    # a sticker: no text field
    await bridge._handle_update(  # pyright: ignore[reportPrivateUsage]
        {"update_id": 2, "message": {"chat": {"id": 1}, "from": {"id": 9}}}
    )
    await bridge._handle_update(  # pyright: ignore[reportPrivateUsage]
        {
            "update_id": 3,
            "message": {
                "text": "/mailflow@other_bot help",
                "chat": {"id": 1, "type": "supergroup"},
                "from": {"id": 9},
            },
        }
    )
    assert _StubClient.posts == []


@pytest.mark.asyncio
async def test_notifier_posts_one_message_per_target(sent: list[Any]) -> None:
    record = _record()
    from mailflow.notifications import DeliveryError

    with pytest.raises(DeliveryError):
        await _notifier().notify(record)
    assert sent == []
    record.manual_urgency = Urgency.URGENT
    notifier = _notifier(bot_token="t", chat_id="42", targets=["user:7", "user:42"])
    await notifier.notify(record)
    assert [call[1] for call in sent] == ["7", "42"]
    assert all(call[0] == "t" for call in sent)
    assert record.mail.subject in sent[0][2]
    assert "urgent" in sent[0][2]
    sent.clear()
    await notifier.push_text("reminder")
    assert [(token, target, text) for token, target, text in sent] == [
        ("t", "7", "reminder"),
        ("t", "42", "reminder"),
    ]
    sent.clear()
    await notifier.push_to_target("user:99", "hourly")
    assert sent == [("t", "99", "hourly")]


@pytest.mark.asyncio
async def test_one_failing_target_does_not_abort_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_module = importlib.import_module("mailflow_notify_telegram.plugin")

    delivered: list[str] = []

    def flaky(token: str, chat_id: str, text: str) -> None:
        if chat_id == "1":
            raise RuntimeError("chat not found")
        delivered.append(chat_id)

    monkeypatch.setattr(plugin_module, "send_text", flaky)
    with pytest.raises(RuntimeError, match="chat not found"):
        await _notifier(bot_token="t", targets=["user:1", "user:2"]).push_text("x")
    assert delivered == ["2"]


async def test_custom_api_url_applies_to_identity_polling_and_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    requests: list[str] = []
    client_type = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json={"ok": True, "result": {"id": 7}})

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return client_type(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    bridge = TelegramBridge("one", "test", "", "http://fake-api/")
    await bridge._load_identity()  # pyright: ignore[reportPrivateUsage]
    await bridge._request("getUpdates")  # pyright: ignore[reportPrivateUsage]
    await bridge._request("sendMessage", {"chat_id": "7", "text": "ready"})  # pyright: ignore[reportPrivateUsage]
    assert requests == [
        f"http://fake-api/bottest/{method}" for method in ("getMe", "getUpdates", "sendMessage")
    ]
    assert bridge.identity["id"] == 7

    sent_to: list[str] = []

    def send(token: str, chat_id: str, text: str, *, api_base: str) -> None:
        sent_to.append(api_base)

    monkeypatch.setattr(
        importlib.import_module("mailflow_notify_telegram.plugin"), "send_text", send
    )
    await _notifier(bot_token="test", chat_id="7", api_base="http://fake-api/").push_text("ready")
    assert sent_to == ["http://fake-api"]

    # Text delivery must expose the same platform retry delay as image delivery.
    from email.message import Message
    from io import BytesIO
    from urllib.error import HTTPError

    from mailflow.notifications import DeliveryError

    gateway = importlib.import_module("mailflow_notify_telegram.gateway")

    def limited(*_args: Any, **_kwargs: Any) -> Any:
        raise HTTPError(
            "http://fake-api",
            429,
            "Too many requests",
            Message(),
            BytesIO(b'{"ok":false,"parameters":{"retry_after":4}}'),
        )

    monkeypatch.setattr(gateway, "urlopen", limited)
    with pytest.raises(DeliveryError) as rejected:
        gateway.send_text("test", "7", "ready", api_base="http://fake-api")
    assert rejected.value.retry_after == 4 and rejected.value.code == "delivery.rate_limited"
