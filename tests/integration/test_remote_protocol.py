"""REST, WebSocket events and remote TUI cooperate through the real ASGI service."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from mailflow.domain import (
    MailRecord,
)
from mailflow.i18n import I18n
from mailflow_server.app import _jsonable, create_app
from mailflow_server.client import RemoteClient
from mailflow_tui.remote import RemoteServiceAdapter
from starlette.websockets import WebSocketDisconnect

from mailflow_test_support.commands import commands_service, make_record


def test_rest_history_live_logs_and_service_events_share_frames() -> None:
    service = commands_service()
    service.config.server.username, service.config.server.password = "admin", "secret"
    app = create_app(service)
    feed = app.state.log_feed
    record = make_record()
    first = logging.LogRecord("mailflow.test", logging.WARNING, "", 0, "historic", (), None)
    feed.handle(first)
    from mailflow_server.client import _basic

    with (
        TestClient(app, headers=_basic("admin", "secret")) as client,
        client.websocket_connect("/ws") as ws,
    ):
        history = client.get("/logs").json()
        ws.send_json({"type": "logs"})
        assert ws.receive_json() == history[0]
        second = logging.LogRecord("mailflow.test", logging.ERROR, "", 0, "live", (), None)
        feed.handle(second)
        frame = ws.receive_json()
        assert frame == client.get("/logs").json()[-1]
        assert frame["event"] == "log" and "|ERROR|mailflow.test|live" in frame["payload"]["line"]
        assert client.portal is not None
        client.portal.call(lambda: service.events.emit("example", nested={"record": record}))
        frame = ws.receive_json()
        assert frame["event"] == "example"
        assert MailRecord.model_validate(frame["payload"]["nested"]["record"]) == record
    assert not feed.queues
    assert not service.events._subscribers.get("*")


def test_websocket_rejects_missing_credentials() -> None:
    service = commands_service()
    service.config.server.username, service.config.server.password = "admin", "secret"
    with (
        TestClient(create_app(service)) as client,
        pytest.raises(WebSocketDisconnect) as error,
        client.websocket_connect("/ws"),
    ):
        pass
    assert error.value.code == 4401


async def test_remote_tui_renders_mail_actions_runtime_and_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import queue

    from mailflow_tui.app import ActionsPane, MailFlowApp, MailPane, RuntimePane
    from mailflow_tui.settings import SettingsPane
    from textual.widgets import DataTable, Static

    service = commands_service()
    service.config.server.username, service.config.server.password = "admin", "secret"
    record = make_record()
    await service.storage.save_mail(record)
    await service.add_action("remote schedule", datetime.now(UTC) + timedelta(days=1))
    real_client = httpx.AsyncClient
    transport = httpx.ASGITransport(app=create_app(service))
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs)
    )
    adapter = RemoteServiceAdapter(
        RemoteClient("http://fake", "admin", "secret"), _jsonable(service.snapshot()), I18n()
    )
    app = MailFlowApp(adapter, queue.Queue(), remote=True)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        mail = app.query_one(MailPane)
        await mail.refresh_mail()
        assert app.query_one("#mail-table", DataTable).row_count == 1
        mail._selected_id = record.record_id
        await mail._show_selected()
        assert record.summary in str(app.query_one("#mail-summary", Static).render())
        await app.query_one(ActionsPane).refresh_actions()
        assert app.query_one("#actions-table", DataTable).row_count == 1
        await app.query_one(RuntimePane).refresh_runtime()
        await app.query_one(SettingsPane).reload()
        assert adapter.settings_sections()
        assert app.sub_title == f"v{service.snapshot().version}"
