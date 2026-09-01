"""OneBot chat-command bridge tests: NapCat message events are received,
forwarded to the injected bot_server endpoint (bot_url), and a non-empty
reply is sent back through the OneBot HTTP API — the notifier's native chat
commands."""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import pytest
from mailflow_notify_onebot.gateway import (
    _OneBotEventBridge,  # pyright: ignore[reportPrivateUsage]
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Recorder:
    """Minimal HTTP server recording requests and answering canned replies."""

    def __init__(self, reply: str = "") -> None:
        self.reply = reply
        self.requests: list[tuple[str, dict[str, Any]]] = []  # (path, body)
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = await reader.readline()
        parts = request_line.decode("utf-8", "replace").strip().split()
        path = parts[1] if len(parts) > 1 else "/"
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        body = await reader.read(65536)
        payload = json.loads(body.decode("utf-8", "replace") or "{}")
        self.requests.append((path, payload))
        resp = json.dumps({"reply": self.reply}).encode("utf-8")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(resp)}\r\nConnection: close\r\n\r\n".encode()
            + resp
        )
        await writer.drain()
        writer.close()


async def _post(bridge_port: int, event: dict[str, Any]) -> None:
    """POST a OneBot event to the bridge and drain the response."""
    async with asyncio.timeout(5):
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge_port)
        body = json.dumps(event).encode("utf-8")
        writer.write(
            b"POST /onebot/event HTTP/1.1\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        while await reader.readline() not in (b"", b"\r\n"):
            pass
        writer.close()


@pytest.mark.asyncio
async def test_bridge_forwards_message_and_sends_reply() -> None:
    """A group message event is forwarded to bot_url; the reply is sent back
    via send_group_msg."""
    bot = _Recorder(reply="Subscribed to mail notifications.")
    onebot = _Recorder()
    await bot.start()
    await onebot.start()
    bridge = _OneBotEventBridge(
        "napcat-1", _free_port(), f"{bot.url}/bot/message", onebot.url, token="sekret"
    )
    await bridge.start()

    await _post(
        bridge.port,
        {
            "post_type": "message",
            "message_type": "group",
            "user_id": 10001,
            "group_id": 20001,
            "raw_message": "/mailflow subscribe",
        },
    )
    await asyncio.sleep(0.2)
    # forwarded to bot_server with chat context
    assert len(bot.requests) == 1
    path, payload = bot.requests[0]
    assert path == "/bot/message"
    assert payload["text"] == "/mailflow subscribe"
    assert payload["sender"] == "10001"
    assert payload["chat_id"] == "20001"
    assert payload["chat_type"] == "group"
    assert payload["provider"] == "napcat"
    assert payload["instance_id"] == "napcat-1"
    # reply sent back via OneBot group API
    assert len(onebot.requests) == 1
    rpath, rpayload = onebot.requests[0]
    assert rpath == "/send_group_msg"
    assert rpayload["group_id"] == 20001
    assert rpayload["message"] == "Subscribed to mail notifications."

    await bridge.stop()
    await bot.stop()
    await onebot.stop()


@pytest.mark.asyncio
async def test_bridge_private_message_and_no_reply() -> None:
    """Private messages use send_private_msg; an empty bot reply means no
    OneBot send at all."""
    bot = _Recorder(reply="")
    onebot = _Recorder()
    await bot.start()
    await onebot.start()
    bridge = _OneBotEventBridge("napcat-1", _free_port(), f"{bot.url}/bot/message", onebot.url)
    await bridge.start()

    await _post(
        bridge.port,
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": 10001,
            "raw_message": "hello not a command",
        },
    )
    await asyncio.sleep(0.2)
    assert len(bot.requests) == 1
    assert bot.requests[0][1]["chat_type"] == "private"
    assert bot.requests[0][1]["chat_id"] == "10001"
    # empty reply -> no OneBot send
    assert onebot.requests == []

    await bridge.stop()
    await bot.stop()
    await onebot.stop()


@pytest.mark.asyncio
async def test_bridge_extracts_text_from_segments() -> None:
    """When raw_message is absent, text segments are joined."""
    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 1,
        "group_id": 2,
        "message": [
            {"type": "text", "data": {"text": "/mailflow "}},
            {"type": "face", "data": {"id": "1"}},
            {"type": "text", "data": {"text": "help"}},
        ],
    }
    extract = _OneBotEventBridge._extract_text  # pyright: ignore[reportPrivateUsage]
    assert extract(event) == "/mailflow help"
    # raw_message wins when present
    event["raw_message"] = "/mailflow status"
    assert extract(event) == "/mailflow status"
