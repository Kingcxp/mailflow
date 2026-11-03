"""HTTP + websocket client for a remote ``mailflow serve`` instance.

``RemoteClient`` mirrors the slice of :class:`MailFlowService` that the
TUI needs, so the app can run against a remote host. Methods that only
make sense locally (mailbox history browsing, gateway management)
raise :class:`RemoteUnsupported`; panes catch it and show the hint.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
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

    code = "common.unsupported"


class RemoteError(RuntimeError):
    def __init__(
        self, status: int, code: str, message: str, params: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.params = params or {}


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
        self.connection_state = "stopped"
        self.retry_after = 0.0

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
                    error = PermissionError(message)
                    error.__dict__.update(code=code, params=dict(failure.get("params") or {}))
                    raise error
                if code == "permission.denied":
                    raise AccessDenied(message)
                if code in {"remote.unsupported", "common.unsupported"}:
                    raise RemoteUnsupported(message)
                if code.startswith("reply."):
                    raise ReplyError(code, message, **dict(failure.get("params") or {}))
                raise RemoteError(
                    response.status_code, code, message, dict(failure.get("params") or {})
                )
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
            await self._set_connection_state("connecting")
            self._ws_task = asyncio.create_task(self._ws_loop())

    async def stop_events(self) -> None:
        if self._ws_task is not None:
            self._ws_task.cancel()
            await asyncio.gather(self._ws_task, return_exceptions=True)
            self._ws_task = None
        await self._set_connection_state("stopped")

    async def _set_connection_state(self, state: str, retry_after: float = 0.0) -> None:
        if (state, retry_after) == (self.connection_state, self.retry_after):
            return
        self.connection_state, self.retry_after = state, retry_after
        # A view callback must not interrupt connection recovery.
        with contextlib.suppress(Exception):
            await self._dispatch(
                "remote.connection.changed", {"state": state, "retry_after": retry_after}
            )

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
            await self._set_connection_state("connecting")
            try:
                async with _ws_connect(url, self._auth) as ws:
                    self._ws, self._logs_sent = ws, False
                    backoff = 1.0
                    await self._set_connection_state("connected")
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
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                self._ws = None
            # A clean close needs the same delay as a network failure.
            await self._set_connection_state("retrying", backoff)
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

    async def smart_action(
        self, instruction: str, *, progress: Any = None, **scope: Any
    ) -> dict[str, Any]:
        started = await self._request("POST", "/smart/jobs", {"instruction": instruction, **scope})
        job_id, cursor = started["job_id"], 0
        try:
            while True:
                status = await self._request("GET", f"/smart/jobs/{job_id}?after={cursor}")
                cursor = int(status["cursor"])
                for event in status["events"]:
                    if progress is not None:
                        detail = event["detail"]
                        if isinstance(detail, list):
                            detail = tuple(cast("list[Any]", detail))
                        progress(event["stage"], event["done"], event["total"], detail)
                if status["state"] == "done":
                    result: dict[str, Any] = status["result"]
                    return result
                if status["state"] != "running":
                    raise RemoteError(400, "smart.model_failed", str(status.get("error") or ""))
                await asyncio.sleep(0.2)
        finally:
            with contextlib.suppress(Exception):
                await self._request("DELETE", f"/smart/jobs/{job_id}")

    async def apply_smart_plan(
        self, plan_id: str, operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        result: dict[str, Any] = await self._request(
            "POST", f"/smart/plans/{plan_id}", {"operations": operations}
        )
        return result

    async def query_mails(self, values: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = await self._request("POST", "/mails/query", values)
        return result

    async def profile(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = await self._request(
            "GET" if values is None else "PUT", "/profile", values
        )
        return result

    async def chat_about_mail(
        self, record_id: str, messages: list[dict[str, str]]
    ) -> dict[str, Any]:
        result: dict[str, Any] = await self._request(
            "POST", f"/mails/{record_id}/chat", {"messages": messages}
        )
        return result

    async def apply_mail_correction(
        self, record_id: str, corrections: dict[str, Any], **values: Any
    ) -> dict[str, Any] | None:
        result: dict[str, Any] | None = await self._request(
            "POST", f"/mails/{record_id}/correction", {"corrections": corrections, **values}
        )
        return result

    async def failed_deliveries(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._request("GET", "/deliveries/failed")
        return result

    async def retry_deliveries(self, delivery_ids: list[str]) -> int:
        result = await self._request("POST", "/deliveries/retry", {"delivery_ids": delivery_ids})
        return int(result["delivered"])

    async def batch_mail(self, record_ids: list[str], **values: Any) -> dict[str, Any]:
        result: dict[str, Any] = await self._request(
            "POST", "/mails/batch", {"record_ids": record_ids, **values}
        )
        return result

    async def set_mail_state(self, record_ids: list[str], **values: Any) -> dict[str, Any]:
        result: dict[str, Any] = await self._request(
            "POST", "/mails/state", {"record_ids": record_ids, **values}
        )
        return result

    async def set_analysis_paused(self, paused: bool) -> dict[str, Any]:
        result: dict[str, Any] = await self._request("POST", "/analysis/pause", {"paused": paused})
        return result

    async def fetch_attachment(self, record_id: str, index: int) -> bytes:
        import base64

        result = await self._request("GET", f"/mails/{record_id}/attachments/{index}")
        return base64.b64decode(result["data"])

    async def list_drafts(self, *, state: str = "") -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._request("GET", "/drafts?state=" + state)
        return result

    async def cleanup_preview(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._request("GET", "/cleanup/preview")
        return result

    async def cleanup_selected(self, record_ids: list[str]) -> dict[str, Any]:
        result: dict[str, Any] = await self._request(
            "POST", "/cleanup/selected", {"record_ids": record_ids}
        )
        return result

    async def run_cleanup(self) -> tuple[int, int]:
        result = await self._request("POST", "/cleanup")
        return int(result["moved"]), int(result["purged"])

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

    async def list_actions(self, *, include_completed: bool = False) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._request(
            "GET", "/actions?include_completed=true" if include_completed else "/actions"
        )
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

    async def create_reply(self, mail_id: str, *, resume: bool = False) -> dict[str, Any]:
        suffix = "?resume=true" if resume else ""
        result: dict[str, Any] = await self._request("POST", f"/mails/{mail_id}/replies{suffix}")
        return result

    async def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        try:
            result: dict[str, Any] = await self._request("GET", f"/drafts/{draft_id}")
            return result
        except ReplyError as exc:
            if exc.code == "reply.draft_not_found":
                return None
            raise
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
