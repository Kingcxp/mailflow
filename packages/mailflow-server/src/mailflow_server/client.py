"""HTTP + websocket client for a remote ``mailflow serve`` instance.

``RemoteClient`` mirrors the slice of :class:`MailFlowService` that the
TUI needs, so the app can run against a remote host. Methods that only
make sense locally (mailbox history browsing, gateway management)
raise :class:`RemoteUnsupported`; panes catch it and show the hint.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
from collections.abc import Callable
from typing import Any, cast

import httpx
from mailflow.domain import ActionItem, MailMessage, MailRecord, ReplyDraft
from mailflow.permissions import AccessDenied
from mailflow.protocol import json_value
from mailflow.replies import ReplyError
from pydantic import BaseModel

_UNSET = object()


class RemoteUnsupported(RuntimeError):
    """The operation requires a locally-attached service."""


class RemoteError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _basic(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _ws_headers(auth: dict[str, str], connect: Any = None) -> dict[str, Any]:
    """Select the header parameter exposed by the installed client API."""
    if connect is None:
        import websockets

        connect = websockets.connect
    parameters = inspect.signature(connect).parameters
    key = "additional_headers" if "additional_headers" in parameters else "extra_headers"
    return {key: dict(auth)}


def _ws_connect(url: str, auth: dict[str, str]) -> Any:
    """Open an authenticated websocket to ``url``.

    The server's ``/ws`` endpoint requires HTTP Basic auth on the upgrade
    handshake (it closes with 4401 otherwise), so the header must be
    attached here — every REST method already sends ``self._auth``.
    """
    import websockets

    return cast(Any, websockets.connect(url, **_ws_headers(auth, websockets.connect)))


class RemoteClient:
    """Thin async wrapper over the REST+WS surface."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._auth = _basic(username, password)
        self._event_handlers: dict[str, list[Callable[..., Any]]] = {}
        self._ws_task: asyncio.Task[Any] | None = None
        self.log_queue: asyncio.Queue[str] = asyncio.Queue()
        self._logs_requested = False
        self._logs_sent = False
        self._ws: Any = None
        self._logs_lock = asyncio.Lock()

    # -- low level -------------------------------------------------------------

    async def _request(self, method: str, path: str, json_body: Any = _UNSET) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            response = await client.request(
                method,
                path,
                headers=self._auth,
                **({"json": json_value(json_body)} if json_body is not _UNSET else {}),
            )
        if response.status_code >= 400:
            detail = ""
            try:
                detail = response.json().get("detail", "")
            except Exception:
                detail = response.text[:200]
            if isinstance(detail, dict):
                failure = cast("dict[str, Any]", detail)
                code = str(failure.get("code", ""))
                message = str(failure.get("message", code))
                if code == "reply.confirm_invalid":
                    raise PermissionError(message)
                if code == "permission.denied":
                    raise AccessDenied(message)
                if code in {"remote.unsupported", "common.unsupported"}:
                    raise RemoteUnsupported(message)
                if code.startswith("reply."):
                    raise ReplyError(code, message, **dict(failure.get("params") or {}))
                raise RemoteError(response.status_code, code, message)
            raise RemoteError(response.status_code, "", str(detail))
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
        models: dict[str, type[BaseModel]] = {
            "mail": MailMessage,
            "record": MailRecord,
            "item": ActionItem,
            "draft": ReplyDraft,
        }
        for key, model in models.items():
            if isinstance(payload.get(key), dict):
                payload[key] = model.model_validate(payload[key])
        if event == "mailflow.action.digest" and isinstance(payload.get("items"), list):
            payload["items"] = [ActionItem.model_validate(item) for item in payload["items"]]
        for handler in [*self._event_handlers.get(event, []), *self._event_handlers.get("*", [])]:
            result = handler(event=event, **payload)
            if asyncio.iscoroutine(result):
                await result

    async def start_events(self) -> None:
        if self._ws_task is None or self._ws_task.done():
            self._ws_task = asyncio.create_task(self._ws_loop())

    async def stop_events(self) -> None:
        if self._ws_task is not None:
            self._ws_task.cancel()
            await asyncio.gather(self._ws_task, return_exceptions=True)
            self._ws_task = None

    async def enable_logs(self) -> None:
        """Ask the persistent event connection to also stream log lines."""
        self._logs_requested = True
        await self._request_logs()

    async def _request_logs(self) -> None:
        async with self._logs_lock:
            if self._ws is not None and self._logs_requested and not self._logs_sent:
                await self._ws.send(json.dumps({"type": "logs"}))
                self._logs_sent = True

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
                    self._ws, self._logs_sent = ws, False
                    # request log streaming once per connection so the remote
                    # TUI's log pane fills even though nothing calls
                    # enable_logs() explicitly today
                    await self._request_logs()
                    async for raw in ws:
                        try:
                            frame: Any = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(frame, dict):
                            continue
                        frame_map = cast("dict[str, Any]", frame)
                        event = str(frame_map.get("event", ""))
                        payload = frame_map.get("payload")
                        if not isinstance(payload, dict):
                            continue
                        payload_map = dict(cast("dict[str, Any]", payload))
                        if event == "log":
                            await self.log_queue.put(str(payload_map.get("line", "")))
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
                self._ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self._ws = None

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
        except RemoteError as exc:
            if exc.status == 404:
                return None
            raise

    async def set_mail_urgency(self, record_id: str, urgency: Any) -> dict[str, Any] | None:
        level = "auto" if urgency is None else getattr(urgency, "value", str(urgency))
        try:
            result: dict[str, Any] = await self._request(
                "POST", f"/mails/{record_id}/urgency", {"urgency": level}
            )
            return result
        except RemoteError as exc:
            if exc.status == 404:
                return None
            raise

    async def delete_mail(self, record_id: str) -> bool:
        try:
            await self._request("DELETE", f"/mails/{record_id}")
        except RemoteError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    async def restore_mail(self, record_id: str) -> dict[str, Any] | None:
        try:
            result: dict[str, Any] = await self._request("POST", f"/trash/{record_id}/restore")
            return result
        except RemoteError as exc:
            if exc.status == 404:
                return None
            raise

    async def list_actions(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._request("GET", "/actions")
        return result

    async def list_failed_mails(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._request("GET", "/mails?view=failed")
        return result

    async def list_expired_mails(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._request("GET", "/mails?view=expired")
        return result

    async def delete_mails(self, record_ids: list[str]) -> int:
        result = await self._request("POST", "/mails/delete", {"record_ids": record_ids})
        return int(result["count"])

    async def purge_expired_mails(self, record_ids: list[str] | None = None) -> int:
        result = await self._request("POST", "/mails/purge-expired", {"record_ids": record_ids})
        return int(result["count"])

    async def add_action(self, values: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = await self._request("POST", "/actions", values)
        return result

    async def edit_action(self, item_id: str, values: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = await self._request("PUT", f"/actions/{item_id}", values)
        return result

    async def delete_action(self, item_id: str) -> bool:
        result = await self._request("DELETE", f"/actions/{item_id}")
        return bool(result["ok"])

    async def create_reply(self, mail_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._request("POST", f"/mails/{mail_id}/replies")
        return result

    async def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        try:
            result: dict[str, Any] = await self._request("GET", f"/drafts/{draft_id}")
            return result
        except RemoteError as exc:
            if exc.status == 404:
                return None
            raise

    async def edit_draft(self, draft_id: str, subject: str, body: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._request(
            "PUT", f"/drafts/{draft_id}", {"subject": subject, "body": body}
        )
        return result

    async def prepare_reply(self, draft_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._request("POST", f"/drafts/{draft_id}/prepare")
        return result

    async def confirm_reply(self, draft_id: str, token: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._request(
            "POST", f"/drafts/{draft_id}/confirm", {"token": token}
        )
        return result

    async def cancel_reply(self, draft_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._request("POST", f"/drafts/{draft_id}/cancel")
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

    async def set_setting(self, key: str, value: Any) -> dict[str, Any] | None:
        result: dict[str, Any] | None = await self._request(
            "PUT", f"/settings/{key}", {"value": value}
        )
        return result

    async def reset_setting(self, key: str) -> dict[str, Any] | None:
        result: dict[str, Any] | None = await self._request("POST", f"/settings/{key}/reset")
        return result

    async def plugin_enable(self, plugin_id: str) -> str:
        result = await self._request("POST", f"/plugins/{plugin_id}/enable")
        return str(result.get("created_instance", ""))

    async def plugin_disable(self, plugin_id: str) -> None:
        await self._request("POST", f"/plugins/{plugin_id}/disable")

    async def plugin_uninstall(self, plugin_id: str) -> str:
        result = await self._request("POST", f"/plugins/{plugin_id}/uninstall")
        return str(result["output"])

    async def plugin_install(self, plugin_id: str) -> str:
        result = await self._request("POST", f"/plugins/{plugin_id}/install")
        return str(result["output"])

    async def test_llm(self, llm_id: str) -> tuple[float, str]:
        started = asyncio.get_running_loop().time()
        result = await self._request("POST", "/llms/test", {"llm_id": llm_id})
        seconds = float(result.get("seconds", asyncio.get_running_loop().time() - started))
        return seconds, str(result.get("reply", ""))


__all__ = ["RemoteClient", "RemoteUnsupported"]
