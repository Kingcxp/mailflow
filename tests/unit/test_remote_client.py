"""Remote values, frames and operations preserve the local service contract."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
from mailflow.domain import (
    ActionItem,
    ActionOrigin,
    Attachment,
    MailMessage,
    MailRecord,
    ProcessorBindingSnapshot,
    ProcessorNote,
    RuntimeSnapshot,
    Urgency,
)
from mailflow.i18n import I18n
from mailflow.protocol import event_frame, json_value
from mailflow_server.app import create_app
from mailflow_server.client import RemoteClient, RemoteUnsupported
from mailflow_tui.remote import RemoteServiceAdapter

from mailflow_test_support.commands import commands_service, make_record
from mailflow_test_support.ui import mock_http_client


def test_recursive_values_include_models_dates_enums_and_settings() -> None:
    @dataclass
    class Nested:
        when: date
        urgency: Urgency

    record = make_record()
    record.mail.attachments = [Attachment(filename="image.png", data=b"\x89PNG\x00\xff")]
    payload = {
        "nested": [{"record": record, "meta": Nested(date(2026, 11, 2), Urgency.URGENT)}],
        "when": datetime(2026, 11, 2, tzinfo=UTC),
        "settings": commands_service().settings_sections(),
    }
    result = json.loads(json.dumps(json_value(payload)))
    assert result["nested"][0]["meta"] == {"when": "2026-11-02", "urgency": "urgent"}
    restored = MailRecord.model_validate(result["nested"][0]["record"])
    assert restored.mail.attachments[0].filename == "image.png"
    assert restored.mail.attachments[0].data is None
    assert record.mail.attachments[0].data == b"\x89PNG\x00\xff"
    assert restored.mail.date == record.mail.date
    assert result["settings"][0]["options"][0]["editor"]


@pytest.mark.parametrize("modern", [False, True])
def test_websocket_connect_sends_only_supported_header_keyword(
    monkeypatch: pytest.MonkeyPatch, modern: bool
) -> None:
    import sys

    captured: list[dict[str, Any]] = []

    def legacy(url: str, *, extra_headers: Any = None, **kwargs: Any) -> object:
        captured.append({"headers": extra_headers, **kwargs})
        return object()

    def current(url: str, *, additional_headers: Any = None, **kwargs: Any) -> object:
        captured.append({"headers": additional_headers, **kwargs})
        return object()

    connect = current if modern else legacy
    auth = {"Authorization": "Basic fake"}
    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    # Exercise the transport adapter at its dependency boundary.
    from mailflow_server.client import (
        _ws_connect,  # pyright: ignore[reportPrivateUsage]
        _ws_headers,  # pyright: ignore[reportPrivateUsage]
    )

    _ws_connect("ws://fake/ws", auth)
    assert captured == [{"headers": auth}]
    assert set(_ws_headers(auth, connect)) == {"additional_headers" if modern else "extra_headers"}


async def test_fake_websocket_live_logs_typed_events_and_repeat_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.incoming: asyncio.Queue[str] = asyncio.Queue()
            self.sent: list[dict[str, Any]] = []
            self.connected = asyncio.Event()

        async def __aenter__(self) -> FakeWebSocket:
            self.connected.set()
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def send(self, frame: str) -> None:
            self.sent.append(json.loads(frame))

        def __aiter__(self) -> FakeWebSocket:
            return self

        async def __anext__(self) -> str:
            return await self.incoming.get()

    ws = FakeWebSocket()
    connect = Mock(return_value=ws)
    monkeypatch.setattr("mailflow_server.client._ws_connect", connect)
    client = RemoteClient("http://fake", "admin", "secret")
    record = make_record()
    records: list[MailRecord] = []
    received = asyncio.Event()
    mails: list[MailMessage] = []
    mail_received = asyncio.Event()

    def processed(event: str, **payload: Any) -> None:
        records.append(payload["record"])
        received.set()

    def incoming(event: str, **payload: Any) -> None:
        mails.append(payload["mail"])
        mail_received.set()

    client.on("mailflow.mail.processed", processed)
    client.on("mailflow.mail.received", incoming)
    await client.start_events()
    await client.start_events()
    try:
        await asyncio.wait_for(ws.connected.wait(), 1)
        await client.enable_logs()
        await client.enable_logs()
        assert ws.sent == [{"type": "logs"}]
        await ws.incoming.put(json.dumps(["invalid frame"]))
        await ws.incoming.put(json.dumps({"event": "invalid", "payload": []}))
        await ws.incoming.put(
            json.dumps(event_frame("log", {"line": "10:00|ERROR|mailflow.test|live"}))
        )
        await ws.incoming.put(
            json.dumps(event_frame("mailflow.mail.processed", {"record": record}))
        )
        await ws.incoming.put(
            json.dumps(event_frame("mailflow.mail.received", {"mail": record.mail}))
        )
        assert await asyncio.wait_for(client.log_queue.get(), 1) == "10:00|ERROR|mailflow.test|live"
        await asyncio.wait_for(received.wait(), 1)
        assert records == [record]
        assert isinstance(records[0].mail.date, datetime)
        await asyncio.wait_for(mail_received.wait(), 1)
        assert mails == [record.mail]
        assert isinstance(mails[0].date, datetime)
        assert connect.call_count == 1
    finally:
        await client.stop_events()
    sent = list(ws.sent)
    await client.enable_logs()
    assert ws.sent == sent


async def test_remote_adapter_uses_real_domain_and_setting_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = commands_service()
    service.config.server.username, service.config.server.password = "admin", "secret"
    record = make_record()
    record.received_at = record.mail.received_at
    record.manual_urgency = Urgency.IMPORTANT
    now = datetime.now(UTC)
    record.processor_notes = [
        ProcessorNote(
            processor_id="rules",
            plugin_id="core",
            status="success",
            started_at=now,
            finished_at=now,
        )
    ]
    await service.storage.save_mail(record)
    transport = httpx.ASGITransport(app=create_app(service))
    mock_http_client(monkeypatch, transport)
    initial = service.snapshot()
    initial.processors = [
        ProcessorBindingSnapshot(
            processor_id="importance",
            plugin_id="core",
            priority=10,
            llm_id="primary",
            fallback_llm_ids=["fallback"],
        )
    ]
    adapter = RemoteServiceAdapter(
        RemoteClient("http://fake", "admin", "secret"), json_value(initial), I18n()
    )
    assert isinstance(adapter.snapshot(), RuntimeSnapshot)
    assert adapter.snapshot().processors == initial.processors
    assert adapter.snapshot().processors[0].fallback_llm_ids == ["fallback"]
    assert await adapter.get_mail(record.record_id) == record
    assert (await adapter.list_mails())[0].mail.sender.display == record.mail.sender.display
    assert (await adapter.list_mails())[0].processor_notes[0].duration_ms == 0
    item = await adapter.add_action("remote action", now + timedelta(days=5), notes="saved")
    assert isinstance(item, ActionItem) and item.origin is ActionOrigin.CUSTOM
    edited = await adapter.edit_action(item.item_id, due_end=item.due_at + timedelta(hours=1))
    assert edited == (await service.list_actions())[0]
    assert edited.time_range_in("Asia/Shanghai")
    assert await adapter.list_actions() == await service.list_actions()
    assert await adapter.delete_action(item.item_id)
    assert not await adapter.list_actions()
    assert await adapter.delete_mail(record.record_id)
    assert (await adapter.list_trash())[0].to_mail_record() == record
    assert await adapter.restore_mail(record.record_id) == record
    assert await adapter.get_mail("missing") is None
    assert not await adapter.delete_mail("missing")
    await adapter.refresh_settings()
    sections = adapter.settings_sections()
    assert json_value(sections) == json_value(service.settings_sections())
    assert (
        sections[0].options[0].display_value()
        == service.settings_sections()[0].options[0].display_value()
    )
    assert not hasattr(adapter, "invented_operation")
    adapter.i18n.set_language("zh-CN")
    assert adapter.display_text("Advertisement detected by rules") == "规则判定为广告"
    assert adapter.display_text("领学生证需要本人到场") == "领学生证需要本人到场"
    with pytest.raises(RemoteUnsupported, match="本地服务"):
        await adapter.fetch_history("account")


async def test_httpx_mock_does_not_confuse_error_text_with_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(503, json={"detail": "worker 404 failed"})
    )
    mock_http_client(monkeypatch, transport)
    with pytest.raises(RuntimeError, match="worker 404 failed"):
        await RemoteClient("http://fake", "admin", "secret").get_mail("mail")
