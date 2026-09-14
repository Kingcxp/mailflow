"""Local HTTP endpoint for chat-platform command dispatch.

Gateway bridges (openwechat, onebot) forward incoming chat
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
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

logger = logging.getLogger("mailflow.bot_server")

if TYPE_CHECKING:
    from mailflow.service import MailFlowService

_HOST = "127.0.0.1"
_PORT = 18789
_MAX_REQUEST_BYTES = 1 << 20


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

    @staticmethod
    async def _read_chunked(reader: asyncio.StreamReader) -> bytes:
        """Read one bounded HTTP chunked body exactly."""
        body = bytearray()
        while True:
            size_line = await reader.readline()
            try:
                size = int(size_line.split(b";", 1)[0].strip(), 16)
            except ValueError as exc:
                raise ValueError("invalid chunked request body") from exc
            if size < 0 or len(body) + size > _MAX_REQUEST_BYTES:
                raise ValueError("request body is too large")
            if size == 0:
                while await reader.readline() not in (b"\r\n", b"\n", b""):
                    pass
                return bytes(body)
            body.extend(await reader.readexactly(size))
            if await reader.readexactly(2) != b"\r\n":
                raise ValueError("invalid chunked request terminator")

    @classmethod
    async def _read_request_body(cls, reader: asyncio.StreamReader) -> bytes:
        """Parse framing headers so a keep-alive client cannot be truncated."""
        content_length: int | None = None
        chunked = False
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, separator, value = line.decode("latin-1").partition(":")
            if not separator:
                raise ValueError("malformed request header")
            if name.strip().casefold() == "content-length":
                try:
                    content_length = int(value.strip())
                except ValueError as exc:
                    raise ValueError("invalid Content-Length") from exc
            elif name.strip().casefold() == "transfer-encoding" and "chunked" in value.casefold():
                chunked = True
        if chunked:
            return await cls._read_chunked(reader)
        if content_length is None:
            raise ValueError("missing request body length")
        if content_length < 0 or content_length > _MAX_REQUEST_BYTES:
            raise ValueError("request body is too large")
        return await reader.readexactly(content_length)

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=15.0)
            parts = request_line.decode("utf-8", "replace").strip().split()
            if len(parts) < 2 or parts[0] != "POST":
                await self._respond(writer, 404, {"reply": ""})
                return
            path = parts[1]
            body = await asyncio.wait_for(self._read_request_body(reader), timeout=20.0)
            payload = json.loads(body.decode("utf-8", "replace") or "{}")
            text = str(payload.get("text") or "")
            if path == "/bot/message":
                instance = str(payload.get("instance_id") or "")
                chat_id = str(payload.get("chat_id") or "")
                # INFO on every chat hop: the chain (platform -> bridge ->
                # here -> command) is otherwise invisible, and a missing
                # log line pinpoints where it broke.
                logger.info(
                    "chat[%s] %s:%s: %.80r",
                    instance,
                    payload.get("chat_type") or "chat",
                    chat_id,
                    text,
                )
                reply = await self._service.command_dispatch(
                    text,
                    sender=str(payload.get("sender") or ""),
                    chat_id=chat_id,
                    chat_type=str(payload.get("chat_type") or ""),
                    provider=str(payload.get("provider") or ""),
                    instance_id=instance,
                )
                if reply:
                    preview = (
                        f"{len(reply)} chunks" if isinstance(reply, list) else repr(reply)[:80]
                    )
                    logger.info("chat[%s] reply to %s: %s", instance, chat_id, preview)
                # Gateway bridges send every returned page in order. Do not
                # truncate here: platform-specific clipping silently loses
                # confirmation tokens and command details.
                payload_reply: Any = self._service.chat_reply_chunks(reply)
                await self._respond(writer, 200, {"reply": payload_reply})
            else:
                await self._respond(writer, 404, {"reply": ""})
        except (TimeoutError, ValueError, json.JSONDecodeError, asyncio.IncompleteReadError) as exc:
            logger.warning("bot endpoint rejected malformed request: %s", exc)
            with contextlib.suppress(Exception):
                await self._respond(writer, 400, {"reply": ""})
        except Exception:
            logger.warning("bot endpoint request failed", exc_info=True)
            with contextlib.suppress(Exception):
                await self._respond(writer, 500, {"reply": ""})
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        reason = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            500: "Internal Server Error",
        }.get(status, "OK")
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n".encode()
        )
        writer.write(body)
        await writer.drain()
