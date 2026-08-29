"""Local HTTP endpoint for chat-platform command dispatch.

Gateway bridges (wechaty, openwechat, onebot) forward incoming chat
messages here: ``POST /bot/message`` with ``{"text": "..."}``. Messages
starting with the configured command prefix are routed through the
CommandRouter; the reply is returned as ``{"reply": "..."}`` so the
bridge can send it back to the chat. Messages without the prefix get an
empty reply and are ignored.

Bound to 127.0.0.1 only — never exposed to the network. Uses only the
standard library (asyncio streams).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

logger = logging.getLogger("mailflow.bot_server")

if TYPE_CHECKING:
    from mailflow.service import MailFlowService

_HOST = "127.0.0.1"
_PORT = 18789


class BotServer:
    """Async HTTP server for chat command dispatch."""

    def __init__(self, service: MailFlowService) -> None:
        self._service = service
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        # bind the configured port, bumping on conflict so parallel test
        # services (and stray zombies) never take the endpoint down
        port = _PORT
        last_exc: OSError | None = None
        for _attempt in range(5):
            try:
                self._server = await asyncio.start_server(self._handle_connection, _HOST, port)
                break
            except OSError as exc:
                last_exc = exc
                port += 1
        if self._server is None:
            raise RuntimeError(
                f"bot endpoint: could not bind {_HOST}:{_PORT}..{port - 1}: {last_exc}"
            )
        self._port = port
        logger.info("bot command endpoint listening on http://%s:%d/bot/message", _HOST, port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def url(self) -> str:
        return f"http://{_HOST}:{getattr(self, '_port', _PORT)}/bot/message"

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await reader.readline()
            parts = request_line.decode("utf-8", "replace").strip().split()
            if len(parts) < 2 or parts[0] != "POST":
                await self._respond(writer, 404, {"reply": ""})
                return
            path = parts[1]
            # consume headers
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            body = await reader.read(65536)
            payload = json.loads(body.decode("utf-8", "replace") or "{}")
            text = str(payload.get("text") or "")
            if path == "/bot/message":
                reply = await self._service.command_dispatch(
                    text,
                    sender=str(payload.get("sender") or ""),
                    chat_id=str(payload.get("chat_id") or ""),
                    chat_type=str(payload.get("chat_type") or ""),
                    provider=str(payload.get("provider") or ""),
                    instance_id=str(payload.get("instance_id") or ""),
                )
                await self._respond(writer, 200, {"reply": reply or ""})
            else:
                await self._respond(writer, 404, {"reply": ""})
        except Exception as exc:
            logger.debug("bot endpoint request failed: %s", exc)
            with __import__("contextlib").suppress(Exception):
                await self._respond(writer, 500, {"reply": ""})
        finally:
            with __import__("contextlib").suppress(Exception):
                writer.close()

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        reason = {200: "OK", 404: "Not Found", 500: "Internal Server Error"}.get(status, "OK")
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n".encode()
        )
        writer.write(body)
        await writer.drain()
