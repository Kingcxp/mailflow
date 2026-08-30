"""HTTP + websocket client for a remote ``mailflow serve`` instance.

``RemoteClient`` mirrors the slice of :class:`MailFlowService` that the
TUI needs, so the app can run against a remote host. Methods that only
make sense locally (mailbox history browsing, marketplace installs)
raise :class:`RemoteUnsupported`; panes catch it and show the hint.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from typing import Any, cast

import httpx

_UNSET = object()


class RemoteUnsupported(RuntimeError):
    """The operation requires a locally-attached service."""


def _basic(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _ws_headers(auth: dict[str, str]) -> dict[str, Any]:
    """Header kwargs accepted by ``websockets.connect`` across versions.

    websockets >= 12 renamed ``extra_headers`` to ``additional_headers``;
    both are passed as the raw header mapping, so trying the newer name
    first and falling back to the legacy one keeps the client working
    with whatever uvicorn[standard] pinned.
    """
    return {"additional_headers": dict(auth), "extra_headers": dict(auth)}


def _ws_connect(url: str, auth: dict[str, str]) -> Any:
    """Open an authenticated websocket to ``url``.

    The server's ``/ws`` endpoint requires HTTP Basic auth on the upgrade
    handshake (it closes with 4401 otherwise), so the header must be
    attached here — every REST method already sends ``self._auth``.
    """
    import websockets  # type: ignore[import-not-found,unused-ignore]

    return cast(Any, websockets.connect(url, **_ws_headers(auth)))  # pyright: ignore[reportUnknownMemberType]


class RemoteClient:
    """Thin async wrapper over the REST+WS surface."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._auth = _basic(username, password)
        self._event_handlers: dict[str, list[Callable[..., Any]]] = {}
        self._ws_task: asyncio.Task[Any] | None = None
        self.log_queue: asyncio.Queue[str] = asyncio.Queue()
        self._logs_requested = False

    # -- low level -------------------------------------------------------------

    async def _request(self, method: str, path: str, json_body: Any = _UNSET) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            response = await client.request(
                method,
                path,
                headers=self._auth,
                **({"json": json_body} if json_body is not _UNSET else {}),
            )
        if response.status_code >= 400:
            detail = ""
            try:
                detail = response.json().get("detail", "")
            except Exception:
                detail = response.text[:200]
            raise RuntimeError(f"HTTP {response.status_code}: {detail}")
        if not response.content:
            return None
        try:
            return response.json()
        except json.JSONDecodeError:
            return response.text

    def on(self, event: str, handler: Callable[..., Any]) -> Callable[[], None]:
        """Register an event handler; '*' receives every server event."""
        handlers = self._event_handlers.setdefault(event, [])
        if handler not in handlers:
            handlers.append(handler)

        def unsubscribe() -> None:
            current = self._event_handlers.get(event, [])
            if handler in current:
                current.remove(handler)

        return unsubscribe

    async def _dispatch(self, event: str, payload: dict[str, Any]) -> None:
        for handler in [*self._event_handlers.get(event, []), *self._event_handlers.get("*", [])]:
            result = handler(event=event, **payload)
            if asyncio.iscoroutine(result):
                await result

    async def start_events(self) -> None:
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def stop_events(self) -> None:
        if self._ws_task is not None:
            self._ws_task.cancel()
            await asyncio.gather(self._ws_task, return_exceptions=True)
            self._ws_task = None

    async def enable_logs(self) -> None:
        """Ask the persistent event connection to also stream log lines."""
        self._logs_requested = True

    async def _send_ws(self, frame: dict[str, Any]) -> None:
        url = self.base_url.replace("http", "ws", 1) + "/ws"
        async with _ws_connect(url, self._auth) as ws:
            await ws.send(json.dumps(frame))

    async def _ws_loop(self) -> None:
        """Persistent event connection with reconnect and optional log fan-out.

        The server only attaches a connection to its log feed after the
        client sends ``{"type": "logs"}``, and it does so per-connection —
        so the frame must go over THIS connection, not a throwaway one.
        """
        url = self.base_url.replace("http", "ws", 1) + "/ws"
        backoff = 1.0
        while True:
            try:
                async with _ws_connect(url, self._auth) as ws:
                    # request log streaming once per connection so the remote
                    # TUI's log pane fills even though nothing calls
                    # enable_logs() explicitly today
                    if self._logs_requested:
                        await ws.send(json.dumps({"type": "logs"}))
                    async for raw in ws:
                        try:
                            frame = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        event = str(frame.get("event", ""))
                        payload_map: dict[str, Any] = dict(frame.get("payload") or {})
                        if event == "log":
                            await self.log_queue.put(str(frame.get("line", "")))
                            continue
                        try:
                            await self._dispatch(event, payload_map)
                        except Exception:
                            # a single bad handler must not kill the stream
                            continue
                    # connection closed cleanly by the server
                    backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                # server down / restart / network blip: reconnect with backoff
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    # -- service-like surface ----------------------------------------------------

    async def snapshot(self) -> dict[str, Any]:
        data: dict[str, Any] = await self._request("GET", "/snapshot")
        return data

    async def list_mails(self, limit: int | None = None) -> list[dict[str, Any]]:
        path = "/mails" if limit is None else f"/mails?limit={limit}"
        result: list[dict[str, Any]] = await self._request("GET", path)
        return result

    async def get_mail(self, record_id: str) -> dict[str, Any] | None:
        try:
            result: dict[str, Any] = await self._request("GET", f"/mails/{record_id}")
            return result
        except RuntimeError as exc:
            if "404" in str(exc):
                return None
            raise

    async def set_mail_urgency(self, record_id: str, urgency: Any) -> dict[str, Any] | None:
        level = "auto" if urgency is None else getattr(urgency, "value", str(urgency))
        try:
            result: dict[str, Any] = await self._request(
                "POST", f"/mails/{record_id}/urgency", {"urgency": level}
            )
            return result
        except RuntimeError as exc:
            if "404" in str(exc):
                return None
            raise

    async def delete_mail(self, record_id: str) -> bool:
        await self._request("DELETE", f"/mails/{record_id}")
        return True

    async def restore_mail(self, record_id: str) -> dict[str, Any] | None:
        try:
            result: dict[str, Any] = await self._request("POST", f"/trash/{record_id}/restore")
            return result
        except RuntimeError as exc:
            if "404" in str(exc):
                return None
            raise

    async def list_actions(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._request("GET", "/actions")
        return result

    async def list_trash(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._request("GET", "/trash")
        return result

    async def execute_command(self, line: str) -> tuple[bool, str]:
        result = await self._request("POST", "/commands", {"line": line})
        ok: bool = result.get("ok", False)
        return ok, str(result.get("text", ""))

    async def settings_sections(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._request("GET", "/settings/sections")
        return result

    async def set_setting(self, key: str, value: Any) -> dict[str, Any]:
        result: dict[str, Any] = await self._request("PUT", f"/settings/{key}", {"value": value})
        return result

    async def plugin_enable(self, plugin_id: str) -> str:
        result = await self._request("POST", f"/plugins/{plugin_id}/enable")
        return str(result.get("created_instance", ""))

    async def plugin_disable(self, plugin_id: str) -> None:
        await self._request("POST", f"/plugins/{plugin_id}/disable")

    async def test_llm(self, llm_id: str) -> tuple[float, str]:
        started = asyncio.get_running_loop().time()
        result = await self._request("POST", "/llms/test", {"llm_id": llm_id})
        seconds = float(result.get("seconds", asyncio.get_running_loop().time() - started))
        return seconds, str(result.get("reply", ""))


__all__ = ["RemoteClient", "RemoteUnsupported"]
