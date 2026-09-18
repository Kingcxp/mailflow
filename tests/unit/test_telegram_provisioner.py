"""Telegram gateway provisioner + notifier tests.

Telegram has no local runtime to install — the Bot API *is* the gateway — so
what these tests pin is the part that can silently break: token validation,
the in-process long-poll bridge that carries chat commands, and the notifier's
skip-without-credentials / one-request-per-target behaviour.
"""

from __future__ import annotations

import importlib
from typing import Any, ClassVar

import pytest
from mailflow.config import NotifierConfig
from mailflow.domain import MailRecord, Urgency
from mailflow_notify_telegram.gateway import (
    TelegramBridge,
    TelegramProvisioner,
    as_object,
    as_text,
)
from mailflow_notify_telegram.plugin import TelegramNotifier, targets_of


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


@pytest.mark.asyncio
async def test_install_requires_a_token() -> None:
    """No token means the setup cannot proceed; the message must say how."""
    prov = TelegramProvisioner()
    with pytest.raises(RuntimeError, match="@BotFather"):
        await prov.install("tg-1", {})


@pytest.mark.asyncio
async def test_install_validates_the_token(api: dict[str, Any]) -> None:
    prov = TelegramProvisioner()
    await prov.install("tg-1", {"bot_token": "123:abc"})
    assert api["calls"][0][0] == "getMe"


@pytest.mark.asyncio
async def test_install_reports_a_rejected_token(api: dict[str, Any]) -> None:
    api["fail"] = "telegram getMe: Unauthorized"
    prov = TelegramProvisioner()
    with pytest.raises(RuntimeError, match="Unauthorized"):
        await prov.install("tg-1", {"bot_token": "bad"})


@pytest.mark.asyncio
async def test_start_without_a_token_is_an_error() -> None:
    prov = TelegramProvisioner()
    with pytest.raises(RuntimeError, match="no bot token"):
        await prov.start("tg-1", {})


@pytest.mark.asyncio
async def test_start_reports_the_bot_identity(api: dict[str, Any]) -> None:
    prov = TelegramProvisioner()
    instance = await prov.start("tg-1", {"bot_token": "123:abc", "bot_url": ""})
    assert instance.status == "running"
    assert instance.extra["username"] == "mf_bot"
    await prov.stop("tg-1")


@pytest.mark.asyncio
async def test_status_is_stopped_without_a_bridge() -> None:
    prov = TelegramProvisioner()
    status = await prov.status("tg-1")
    assert status.status == "stopped"
    assert status.error


@pytest.mark.asyncio
async def test_status_reports_running_while_polling(api: dict[str, Any]) -> None:
    prov = TelegramProvisioner()
    await prov.start("tg-1", {"bot_token": "123:abc", "bot_url": ""})
    status = await prov.status("tg-1")
    assert status.status == "running"
    await prov.stop("tg-1")
    assert (await prov.status("tg-1")).status == "stopped"


@pytest.mark.asyncio
async def test_ensure_bridge_restarts_the_poller(api: dict[str, Any]) -> None:
    """After an app restart the instance is still 'running' but the
    in-process poller is gone; ensure_bridge must bring it back."""
    prov = TelegramProvisioner()
    await prov.ensure_bridge("tg-1", {"bot_token": "123:abc", "bot_url": ""})
    assert (await prov.status("tg-1")).status == "running"
    await prov.stop("tg-1")


@pytest.mark.asyncio
async def test_ensure_bridge_without_a_token_warns_and_does_nothing(
    api: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    prov = TelegramProvisioner()
    await prov.ensure_bridge("tg-1", {})
    assert (await prov.status("tg-1")).status == "stopped"


@pytest.mark.asyncio
async def test_qr_returns_the_logged_in_sentinel() -> None:
    """Telegram has no QR login: the guided setup must complete at once
    instead of waiting out the QR timeout."""
    prov = TelegramProvisioner()
    assert await prov.qr("tg-1") == "__MAILFLOW_LOGGED_IN__"


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
    assert _StubClient.posts == []


@pytest.mark.asyncio
async def test_notifier_skips_without_credentials(sent: list[Any]) -> None:
    await _notifier().notify(_record())
    assert sent == []


@pytest.mark.asyncio
async def test_notifier_posts_one_message_per_target(sent: list[Any]) -> None:
    record = _record()
    await _notifier(bot_token="t", chat_id="42", targets=["user:7"]).notify(record)
    assert [call[1] for call in sent] == ["7", "42"]
    assert all(call[0] == "t" for call in sent)
    assert record.mail.subject in sent[0][2]


@pytest.mark.asyncio
async def test_notifier_deduplicates_chat_id_against_targets(sent: list[Any]) -> None:
    """chat_id is the single-destination form of the same setting: a chat
    already in targets must not receive the alert twice."""
    config = NotifierConfig(
        notifier_id="tg",
        provider="telegram",
        options={"bot_token": "t", "chat_id": "42", "targets": ["user:42"]},
    )
    assert targets_of(config) == ["user:42"]


@pytest.mark.asyncio
async def test_push_text_reaches_every_target(sent: list[Any]) -> None:
    await _notifier(bot_token="t", targets=["user:1", "user:2"]).push_text("reminder")
    assert [call[1] for call in sent] == ["1", "2"]


@pytest.mark.asyncio
async def test_push_to_target_strips_the_prefix(sent: list[Any]) -> None:
    await _notifier(bot_token="t").push_to_target("user:99", "hourly")
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
    await _notifier(bot_token="t", targets=["user:1", "user:2"]).push_text("x")
    assert delivered == ["2"]


def test_narrowing_helpers_are_total() -> None:
    """The Bot API is untyped; the helpers must never raise on odd payloads."""
    assert as_object(None) == {}
    assert as_object([1]) == {}
    assert as_object({"a": 1}) == {"a": 1}
    assert as_text(None) == ""
    assert as_text(42) == "42"


def test_urgency_is_visible_in_the_alertas_text(sent: list[Any]) -> None:
    """The urgency must be in the preview, not only in the app."""
    import asyncio

    record = _record()
    record.manual_urgency = Urgency.URGENT
    asyncio.run(_notifier(bot_token="t", chat_id="1").notify(record))
    assert "urgent" in sent[0][2]
