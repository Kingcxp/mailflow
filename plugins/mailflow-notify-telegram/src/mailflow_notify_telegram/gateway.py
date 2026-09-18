"""Telegram gateway provisioner: Bot API long-polling bridge, no runtime install.

Telegram needs no local bot process — the Bot API *is* the gateway — so this
provisioner installs nothing and starts nothing external. What it does provide
is the piece that makes chat commands work: an in-process long-poll loop that
reads updates from ``getUpdates`` and forwards each text message to the local
``mailflow.bot_server`` command endpoint, sending the reply back through
``sendMessage``. The notifier half posts mail alerts the same way.

Because the bridge lives in the MailFlow process, ``ensure_bridge`` recreates
it after an app restart when the instance is already marked running.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, cast
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import httpx
from mailflow.contracts import GatewayInstance, GatewayProvisioner

logger = logging.getLogger("mailflow.gateway.telegram")

_API_ROOT = "https://api.telegram.org"
_POLL_TIMEOUT = 25  # getUpdates long-poll window (seconds)
_RETRY_DELAY = 5.0


def _token_of(options: dict[str, Any]) -> str:
    return str(options.get("bot_token") or "").strip()


def _api(token: str, method: str) -> str:
    return f"{_API_ROOT}/bot{token}/{method}"


def as_object(value: Any) -> dict[str, Any]:
    """Narrow an untyped JSON value to a mapping (Bot API payloads are free-form).

    The ``cast`` is what keeps pyright's strict mode happy: an ``isinstance``
    check on ``Any`` still yields ``dict[Unknown, Unknown]``.
    """
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


async def _call(token: str, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """One Bot API call; raises with the API's own description on failure."""
    async with httpx.AsyncClient(timeout=float(_POLL_TIMEOUT) + 10.0) as client:
        response = await client.post(_api(token, method), json=payload or {})
    try:
        body: Any = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"telegram {method}: non-JSON reply (HTTP {response.status_code})"
        ) from exc
    result = as_object(body)
    if not result:
        raise RuntimeError(f"telegram {method}: unexpected reply shape")
    if not result.get("ok"):
        raise RuntimeError(
            f"telegram {method}: {as_text(result.get('description')) or 'request rejected'}"
        )
    return result


class TelegramBridge:
    """In-process getUpdates long-poll that feeds the MailFlow command router."""

    def __init__(self, instance_id: str, token: str, bot_url: str) -> None:
        self._instance_id = instance_id
        self._token = token
        self._bot_url = bot_url
        self._task: asyncio.Task[Any] | None = None
        self._offset = 0
        self._stopping = False
        self.identity: dict[str, Any] = {}

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stopping = False
        await self._load_identity()
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"telegram-bridge-{self._instance_id}"
        )

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _load_identity(self) -> None:
        """Cache the bot's own id so self-messages are never dispatched."""
        try:
            result = await _call(self._token, "getMe")
            self.identity = as_object(result.get("result"))
        except Exception as exc:
            logger.warning("telegram %s: getMe failed: %s", self._instance_id, exc)
            self.identity = {}

    async def _poll_loop(self) -> None:
        while not self._stopping:
            try:
                result = await _call(
                    self._token,
                    "getUpdates",
                    {
                        "offset": self._offset,
                        "timeout": _POLL_TIMEOUT,
                        "allowed_updates": ["message"],
                    },
                )
                raw_updates: Any = result.get("result")
                updates: list[Any] = (
                    cast("list[Any]", raw_updates) if isinstance(raw_updates, list) else []
                )
                for update in updates:
                    await self._handle_update(as_object(update))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("telegram %s: poll failed: %s", self._instance_id, exc)
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.sleep(_RETRY_DELAY), timeout=_RETRY_DELAY)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        raw_id: Any = update.get("update_id")
        if isinstance(raw_id, int):
            self._offset = max(self._offset, raw_id + 1)
        raw_message: Any = update.get("message")
        if not isinstance(raw_message, dict):
            return
        message = as_object(raw_message)
        text = as_text(message.get("text"))
        if not text:
            return
        chat = as_object(message.get("chat"))
        chat_id = as_text(chat.get("id"))
        if not chat_id:
            return
        sender_id = as_text(as_object(message.get("from")).get("id"))
        bot_id = as_text(self.identity.get("id"))
        if bot_id and sender_id == bot_id:
            return
        chat_type = "group" if as_text(chat.get("type")) in {"group", "supergroup"} else "private"
        reply = await self._dispatch(text, sender_id, chat_id, chat_type)
        for page in reply:
            try:
                await _call(self._token, "sendMessage", {"chat_id": chat_id, "text": page})
            except Exception as exc:
                logger.warning(
                    "telegram %s: reply to %s failed: %s", self._instance_id, chat_id, exc
                )

    async def _dispatch(self, text: str, sender: str, chat_id: str, chat_type: str) -> list[str]:
        """POST one inbound message to the MailFlow command endpoint."""
        if not self._bot_url:
            return []
        payload = {
            "text": text,
            "sender": sender,
            "chat_id": chat_id,
            "chat_type": chat_type,
            "provider": "telegram",
            "instance_id": self._instance_id,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(self._bot_url, json=payload)
            body: Any = response.json()
        except Exception as exc:
            logger.warning("telegram %s: command dispatch failed: %s", self._instance_id, exc)
            return []
        reply: Any = as_object(body).get("reply")
        if isinstance(reply, str):
            return [reply] if reply else []
        if isinstance(reply, list):
            pages = cast("list[Any]", reply)
            return [as_text(page) for page in pages if as_text(page)]
        return []


class TelegramProvisioner(GatewayProvisioner):
    provider = "telegram"

    def _bridge(self, instance_id: str, options: dict[str, Any]) -> TelegramBridge | None:
        token = _token_of(options)
        if not token:
            return None
        bridges: dict[str, TelegramBridge] = getattr(self, "_bridges", {})
        bridge = bridges.get(instance_id)
        if bridge is None or bridge._token != token:  # pyright: ignore[reportPrivateUsage]
            bridge = TelegramBridge(instance_id, token, str(options.get("bot_url") or ""))
            bridges[instance_id] = bridge
            self._bridges = bridges
        return bridge

    async def detect(self) -> str:
        """Bot API reachability — nothing to install for Telegram."""
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(f"{_API_ROOT}/")
            reachable = response.status_code < 500
        except Exception as exc:
            return f"api.telegram.org unreachable ({type(exc).__name__})"
        return "api.telegram.org reachable" if reachable else "api.telegram.org error"

    async def install(self, instance_id: str, options: dict[str, Any]) -> None:
        """Validate the bot token; Telegram itself needs no local install."""
        progress: Any = options.get("_progress")
        if progress is not None:
            progress.update(10.0, "checking bot token", "detecting")
        token = _token_of(options)
        if not token:
            raise RuntimeError(
                "telegram needs a bot token from @BotFather — open the form, "
                "paste the token, and run the setup again"
            )
        result = await _call(token, "getMe")
        username = as_text(as_object(result.get("result")).get("username"))
        logger.info("telegram %s: token accepted (@%s)", instance_id, username or "bot")
        if progress is not None:
            progress.update(
                100.0, f"token accepted (@{username})" if username else "token accepted", "done"
            )

    async def start(self, instance_id: str, options: dict[str, Any]) -> GatewayInstance:
        token = _token_of(options)
        if not token:
            raise RuntimeError("telegram instance has no bot token; run the setup again")
        bridge = self._bridge(instance_id, options)
        if bridge is None:
            raise RuntimeError("telegram bridge could not be created (missing token)")
        await bridge.start()
        identity = bridge.identity
        return GatewayInstance(
            provider="telegram",
            instance_id=instance_id,
            status="running",
            # Telegram has no local endpoint: the "endpoint" is the API base
            # the notifier posts to, which is also what the form stores.
            endpoint=_API_ROOT,
            extra={
                "username": as_text(identity.get("username")),
                "bot_id": as_text(identity.get("id")),
                "in_process": True,
            },
        )

    async def ensure_bridge(self, instance_id: str, options: dict[str, Any]) -> Any:
        """Recreate the long-poll loop after an app restart."""
        bridge = self._bridge(instance_id, options)
        if bridge is None:
            logger.warning("telegram %s: no token in options; bridge not started", instance_id)
            return None
        if not bridge.running:
            await bridge.start()
        return bridge

    async def stop(self, instance_id: str) -> None:
        bridges: dict[str, TelegramBridge] = getattr(self, "_bridges", {})
        bridge = bridges.pop(instance_id, None)
        if bridge is not None:
            await bridge.stop()

    async def status(self, instance_id: str) -> GatewayInstance:
        bridges: dict[str, TelegramBridge] = getattr(self, "_bridges", {})
        bridge = bridges.get(instance_id)
        if bridge is None:
            return GatewayInstance(
                provider="telegram",
                instance_id=instance_id,
                status="stopped",
                error="bridge not started",
            )
        if not bridge.running:
            return GatewayInstance(
                provider="telegram",
                instance_id=instance_id,
                status="stopped",
                error="bridge not polling",
            )
        return GatewayInstance(
            provider="telegram",
            instance_id=instance_id,
            status="running",
            endpoint=_API_ROOT,
            extra={"in_process": True},
        )

    async def qr(self, instance_id: str) -> str:
        """Telegram has no QR login; the bot token is the credential.

        Returning the logged-in sentinel lets the guided setup complete
        immediately instead of waiting out the QR timeout.
        """
        del instance_id  # protocol shape; Telegram has no per-instance QR
        return "__MAILFLOW_LOGGED_IN__"


def send_text(token: str, chat_id: str, text: str) -> None:
    """Blocking one-shot sendMessage used by the notifier's worker thread."""
    payload = urlencode({"chat_id": chat_id, "text": text}).encode()
    request = Request(
        _api(token, "sendMessage"),
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(request, timeout=15) as response:
        response.read()


__all__ = [
    "TelegramBridge",
    "TelegramProvisioner",
    "as_object",
    "as_text",
    "send_text",
]
