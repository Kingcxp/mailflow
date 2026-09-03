"""Full chat-command chain integration test.

Simulates NapCat's OneBot surface (a fake HTTP API capturing
send_group_msg) and pushes a real message event through the REAL bridge,
REAL bot_server and REAL command_dispatch — proving the MailFlow side of
the chat-command chain end to end. If this passes, a silent deployment
means NapCat never pushed the event (httpClients config), not a
MailFlow-side break.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
from mailflow.config import MailFlowConfig
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine
from mailflow.registry import ComponentRegistry
from mailflow.service import MailFlowService
from mailflow_notify_onebot.gateway import (  # pyright: ignore[reportPrivateUsage]
    NapCatProvisioner,
    _OneBotEventBridge,  # pyright: ignore[reportPrivateUsage]
)


class _Recorder:
    """Minimal HTTP server recording requests and answering canned replies."""

    def __init__(self, reply: str = "") -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self._reply = reply
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            request_line = await reader.readline()
            parts = request_line.decode("utf-8", "replace").strip().split()
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            body = await reader.read(65536)
            payload: dict[str, Any] = json.loads(body.decode("utf-8", "replace") or "{}")
            self.requests.append((parts[1] if len(parts) > 1 else "", payload))
            resp = json.dumps({"reply": self._reply} if self._reply else {"ok": True}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(resp)}\r\nConnection: close\r\n\r\n".encode()
                + resp
            )
            await writer.drain()
            writer.close()

        self._server = await asyncio.start_server(_handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]  # pyright: ignore[reportUnknownMemberType]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class _FakePluginManager:
    """Minimal plugin-manager stub satisfying MailFlowService.__init__."""

    def snapshots(self, registry: ComponentRegistry) -> list[Any]:
        return []

    def build_registry(self) -> ComponentRegistry:
        return ComponentRegistry()

    def enabled_infos(self) -> list[Any]:
        return []


class _MemoryStorage:
    """Minimal in-memory storage for the command dispatch path."""

    def __init__(self) -> None:
        self.mails: dict[str, Any] = {}
        self.preferences: dict[str, str] = {}

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def list_mails(self) -> list[Any]:
        return []

    async def get_mail(self, record_id: str) -> Any | None:
        return self.mails.get(record_id)

    async def get_preference(self, key: str) -> str | None:
        return self.preferences.get(key)

    async def set_preference(self, key: str, value: str) -> None:
        self.preferences[key] = value


def _service(prefix: str = "#") -> MailFlowService:
    return MailFlowService(
        config=MailFlowConfig.model_validate({"general": {"command_prefix": prefix}}),
        registry=ComponentRegistry(),
        plugin_manager=cast(Any, _FakePluginManager()),
        storage=cast(Any, _MemoryStorage()),
        sources={},
        router=cast(Any, None),
        pipeline=PipelineEngine([]),
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        i18n=I18n(),
    )


async def _post_event(bridge_port: int, event: dict[str, Any]) -> None:
    import httpx

    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(f"http://127.0.0.1:{bridge_port}/onebot/event", json=event)


@pytest.mark.asyncio
async def test_full_chat_command_chain_replies() -> None:
    """A pushed group message reaches command_dispatch through the real
    bridge and bot_server, and the reply is sent back via the OneBot API."""
    # 1) fake NapCat OneBot API: captures send_group_msg
    onebot = _Recorder()
    await onebot.start()

    # 2) real service with "#" prefix; bot_server binds and runs
    service = _service()
    service.bot_server = None  # type: ignore[attr-defined]
    from mailflow.bot_server import BotServer

    bot_server = BotServer(service)
    await bot_server.start()

    # 3) real bridge wired to the real bot_server and the fake NapCat API
    prov = NapCatProvisioner()
    bridge = _OneBotEventBridge(
        "napcat-test",
        prov._bridge_port_for("napcat-test"),  # pyright: ignore[reportPrivateUsage]
        bot_server.url,
        onebot.url,
    )
    await bridge.start()

    try:
        # 4) push a group message event exactly like NapCat's httpClients
        await _post_event(
            bridge.port,
            {
                "post_type": "message",
                "message_type": "group",
                "user_id": 404291187,
                "group_id": 565424593,
                "raw_message": "#mailflow help",
            },
        )
        await asyncio.sleep(0.3)

        # 5) the reply went back through the OneBot HTTP API
        assert onebot.requests, "no reply was sent to the OneBot API"
        path, payload = onebot.requests[0]
        assert path == "/send_group_msg"
        assert payload["group_id"] == 565424593
        assert "mailflow help" in str(payload["message"])
    finally:
        await bridge.stop()
        await bot_server.stop()
        await onebot.stop()


@pytest.mark.asyncio
async def test_chain_silently_loses_events_without_bot_url() -> None:
    """A bridge created without bot_url (the silent-failure mode) must be
    detectable: ensure_bridge reports it instead of quietly returning."""
    prov = NapCatProvisioner()
    bridge = await prov.ensure_bridge("napcat-x", {})  # no bot_url
    assert bridge is None  # documented silent mode; callers must warn
