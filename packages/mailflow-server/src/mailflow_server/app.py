"""FastAPI application exposing one MailFlowService to remote clients.

Surface (all behind HTTP Basic auth):
- ``GET  /snapshot`` — the runtime snapshot
- ``GET  /mails`` — stored mail records (optionally limited)
- ``GET  /mails/{id}`` — one record
- ``POST /mails/{id}/urgency`` ``{"urgency": "urgent" | "auto" | ...}``
- ``DELETE /mails/{id}`` — move to trash
- ``GET  /actions`` / ``GET /trash`` / ``POST /trash/{id}/restore``
- ``POST /commands`` ``{"line": "mail list"}`` — shared command router
- ``GET  /settings/sections`` — the editor model
- ``PUT  /settings/{key}`` ``{"value": ...}`` — validated edit (hot-reloads)
- ``POST /plugins/{id}/enable`` / ``disable``
- ``POST /llms/test`` ``{"llm_id": "..."}`` — ping a configured LLM
- ``WS   /ws`` — every service event as JSON; client may send
  ``{"type":"logs"}`` to also receive log lines (recent ones replayed).

The core stays host-independent: only this package imports FastAPI.
"""

# pyright: basic

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime
from threading import RLock
from typing import TYPE_CHECKING, Any

from fastapi import (  # pyright: ignore[reportMissingTypeStubs]
    Depends,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
)
from fastapi.responses import JSONResponse  # pyright: ignore[reportMissingTypeStubs]
from mailflow.permissions import AccessDenied, PermissionContext, permission_scope, require_admin
from mailflow.protocol import event_frame, json_value
from mailflow.replies import ReplyError
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from mailflow_server.auth import check_basic_auth, require_credentials

logger = logging.getLogger("mailflow.server")

if TYPE_CHECKING:
    from mailflow.service import MailFlowService

_LOG_BUFFER = 500


class ActionCreate(BaseModel):
    summary: str
    due_at: datetime
    action_type: str = "errand"
    due_end: datetime | None = None
    notes: str = ""
    location: str = ""
    url: str = ""


class ActionEdit(BaseModel):
    summary: str | None = None
    due_at: datetime | None = None
    action_type: str | None = None
    due_end: datetime | None = None
    clear_end: bool = False
    notes: str | None = None
    location: str | None = None
    url: str | None = None


class _LogFeed(logging.Handler):
    """Fan-out handler feeding connected websocket clients."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(
            logging.Formatter("%(asctime)s|%(levelname)s|%(name)s|%(message)s", "%H:%M:%S")
        )
        self.recent: deque[dict[str, Any]] = deque(maxlen=_LOG_BUFFER)
        self.queues: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[Any]]] = []
        self._lock = RLock()

    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record)
        frame = event_frame("log", {"line": line})
        with self._lock:
            self.recent.append(frame)
            listeners = list(self.queues)
        for loop, queue in listeners:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(queue.put_nowait, frame)

    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.recent)

    def subscribe(self, queue: asyncio.Queue[Any]) -> list[dict[str, Any]]:
        with self._lock:
            self.queues.append((asyncio.get_running_loop(), queue))
            return list(self.recent)

    def unsubscribe(self, queue: asyncio.Queue[Any]) -> None:
        with self._lock:
            self.queues[:] = [(loop, q) for loop, q in self.queues if q is not queue]


def _jsonable(payload: Any) -> Any:
    return json_value(payload)


def create_app(service: MailFlowService) -> FastAPI:
    feed = _LogFeed()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        mailflow_logger = logging.getLogger("mailflow")
        runtime_logs = getattr(service, "_logging_runtime", None)
        if runtime_logs is not None and runtime_logs.redaction_filter is not None:
            feed.addFilter(runtime_logs.redaction_filter)
        mailflow_logger.addHandler(feed)
        try:
            yield
        finally:
            mailflow_logger.removeHandler(feed)
            feed.close()

    app = FastAPI(title="MailFlow server", version="0.1.0", lifespan=lifespan)
    app.state.log_feed = feed
    username, password = require_credentials(service.config.server)

    async def authenticate(connection: HTTPConnection) -> PermissionContext:
        try:
            check_basic_auth(connection, username, password)
        except HTTPException:
            if connection.scope["type"] == "websocket":
                raise WebSocketException(
                    code=4401, reason=service.t("permission.unauthorized")
                ) from None
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "permission.unauthorized",
                    "message": service.t("permission.unauthorized"),
                },
                headers={"WWW-Authenticate": "Basic"},
            ) from None
        return PermissionContext(sender=username, provider="server", is_admin=True)

    identity_dependency = Depends(authenticate)

    async def guard(context: PermissionContext = identity_dependency) -> AsyncIterator[None]:
        with permission_scope(context):
            yield

    # Hosts may replace identity resolution; the shared access policy still runs.
    app.state.authenticate = authenticate

    # ------------------------------------------------------------------ REST

    @app.exception_handler(AccessDenied)
    async def permission_failure(_: Request, exc: AccessDenied) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={
                "detail": {
                    "code": exc.code,
                    "params": exc.params,
                    "message": str(exc),
                }
            },
        )

    @app.exception_handler(ValueError)
    async def invalid_value(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(NotImplementedError)
    async def unsupported_operation(_: Request, exc: NotImplementedError) -> JSONResponse:
        return JSONResponse(
            status_code=501,
            content={
                "detail": {
                    "code": "common.unsupported",
                    "message": service.t("common.unsupported"),
                }
            },
        )

    @app.exception_handler(ReplyError)
    async def reply_failure(_: Request, exc: ReplyError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "detail": {
                    "code": exc.code,
                    "params": exc.params,
                    "message": str(exc),
                }
            },
        )

    async def reply_result(operation: Any) -> Any:
        try:
            return _jsonable(await operation)
        except AccessDenied:
            raise
        except PermissionError:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "reply.confirm_invalid",
                    "message": service.t("reply.confirm_invalid"),
                },
            ) from None
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "reply.not_found",
                    "message": str(exc),
                },
            ) from None

    @app.post("/mails/{record_id}/replies")
    async def create_reply(record_id: str, _: None = Depends(guard)) -> Any:
        return await reply_result(service.create_reply(record_id))

    @app.get("/drafts/{draft_id}")
    async def get_draft(draft_id: str, _: None = Depends(guard)) -> Any:
        draft = await service.get_draft(draft_id)
        if draft is None:
            raise HTTPException(
                status_code=404, detail=service.t("reply.draft_not_found", draft_id=draft_id)
            )
        return _jsonable(draft)

    @app.put("/drafts/{draft_id}")
    async def edit_draft(draft_id: str, body: dict[str, Any], _: None = Depends(guard)) -> Any:
        return await reply_result(
            service.edit_draft(draft_id, str(body.get("subject", "")), str(body.get("body", "")))
        )

    @app.post("/drafts/{draft_id}/prepare")
    async def prepare_reply(draft_id: str, _: None = Depends(guard)) -> Any:
        return await reply_result(service.prepare_reply(draft_id))

    @app.post("/drafts/{draft_id}/confirm")
    async def confirm_reply(draft_id: str, body: dict[str, Any], _: None = Depends(guard)) -> Any:
        return await reply_result(service.confirm_reply(draft_id, str(body.get("token", ""))))

    @app.post("/drafts/{draft_id}/cancel")
    async def cancel_reply(draft_id: str, _: None = Depends(guard)) -> Any:
        return await reply_result(service.cancel_reply(draft_id))

    @app.get("/snapshot")
    def snapshot(_: None = Depends(guard)) -> Any:
        return _jsonable(service.snapshot())

    @app.get("/mails")
    async def mails(limit: int | None = None, view: str = "", _: None = Depends(guard)) -> Any:
        # async route: the service's storage lock is bound to the server's
        # event loop; asyncio.run in a sync route would attach a new loop
        # and fail with "attached to a different loop"
        if view == "failed":
            records = await service.list_failed_mails()
        elif view == "expired":
            records = await service.list_expired_mails()
        else:
            records = await service.list_mails(limit=limit)
        return JSONResponse(_jsonable(records))

    @app.get("/mails/{record_id}")
    async def get_mail(record_id: str, _: None = Depends(guard)) -> Any:
        record = await service.get_mail(record_id)
        if record is None:
            raise HTTPException(
                status_code=404, detail=service.t("mail.not_found", mail_id=record_id)
            )
        return _jsonable(record)

    @app.post("/mails/{record_id}/urgency")
    async def set_urgency(record_id: str, body: dict[str, Any], _: None = Depends(guard)) -> Any:
        from mailflow.domain import parse_urgency

        level = str(body.get("urgency", ""))
        urgency = None if level == "auto" else parse_urgency(level)
        record = await service.set_mail_urgency(record_id, urgency)
        if record is None:
            raise HTTPException(
                status_code=404, detail=service.t("mail.not_found", mail_id=record_id)
            )
        return _jsonable(record)

    @app.delete("/mails/{record_id}")
    async def delete_mail(record_id: str, _: None = Depends(guard)) -> Any:
        if not await service.delete_mail(record_id):
            raise HTTPException(
                status_code=404, detail=service.t("mail.not_found", mail_id=record_id)
            )
        return {"ok": True}

    @app.post("/mails/delete")
    async def delete_mails(body: dict[str, Any], _: None = Depends(guard)) -> Any:
        return {"count": await service.delete_mails(body.get("record_ids", []))}

    @app.post("/mails/purge-expired")
    async def purge_expired(body: dict[str, Any], _: None = Depends(guard)) -> Any:
        return {"count": await service.purge_expired_mails(body.get("record_ids"))}

    @app.get("/actions")
    async def actions(_: None = Depends(guard)) -> Any:
        return _jsonable(await service.list_actions())

    @app.post("/actions")
    async def add_action(body: ActionCreate, _: None = Depends(guard)) -> Any:
        return _jsonable(await service.add_action(**body.model_dump()))

    @app.put("/actions/{item_id}")
    async def edit_action(item_id: str, body: ActionEdit, _: None = Depends(guard)) -> Any:
        return _jsonable(await service.edit_action(item_id, **body.model_dump(exclude_unset=True)))

    @app.delete("/actions/{item_id}")
    async def delete_action(item_id: str, _: None = Depends(guard)) -> Any:
        return {"ok": await service.delete_action(item_id)}

    @app.get("/trash")
    async def trash(_: None = Depends(guard)) -> Any:
        return _jsonable(await service.list_trash())

    @app.post("/trash/{record_id}/restore")
    async def restore(record_id: str, _: None = Depends(guard)) -> Any:
        record = await service.restore_mail(record_id)
        if record is None:
            raise HTTPException(
                status_code=404, detail=service.t("trash.not_found", record_id=record_id)
            )
        return _jsonable(record)

    @app.post("/commands")
    async def commands(body: dict[str, Any], _: None = Depends(guard)) -> Any:
        router = service.commands
        if router is None:
            from mailflow.commands import CommandRouter

            router = CommandRouter(service)
        response = await router.execute(str(body.get("line", "")))
        return {"ok": response.ok, "text": response.text}

    @app.get("/settings/sections")
    def settings_sections(_: None = Depends(guard)) -> Any:
        require_admin(service.t)
        return _jsonable(service.settings_sections())

    @app.put("/settings/{key:path}")
    async def put_setting(key: str, body: dict[str, Any], _: None = Depends(guard)) -> Any:
        from mailflow.settings import SettingsError

        try:
            spec = await service.set_setting(key, body.get("value"))
        except SettingsError as exc:
            raise HTTPException(status_code=400, detail=f"{exc.option}: {exc.message}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _jsonable(spec)

    @app.post("/settings/{key:path}/reset")
    async def reset_setting(key: str, _: None = Depends(guard)) -> Any:
        return _jsonable(await service.reset_setting(key))

    @app.post("/plugins/{plugin_id}/enable")
    async def enable_plugin(plugin_id: str, _: None = Depends(guard)) -> Any:
        created = await service.plugin_enable(plugin_id)
        return {"ok": True, "created_instance": created}

    @app.post("/plugins/{plugin_id}/disable")
    async def disable_plugin(plugin_id: str, _: None = Depends(guard)) -> Any:
        await service.plugin_disable(plugin_id)
        return {"ok": True}

    @app.post("/plugins/{plugin_id}/uninstall")
    async def uninstall_plugin(plugin_id: str, _: None = Depends(guard)) -> Any:
        return {"output": await service.plugin_uninstall(plugin_id)}

    @app.post("/plugins/{plugin_id}/install")
    async def install_plugin(plugin_id: str, _: None = Depends(guard)) -> Any:
        return {"output": await service.plugin_install(plugin_id)}

    @app.post("/llms/test")
    async def test_llm(body: dict[str, Any], _: None = Depends(guard)) -> Any:
        require_admin(service.t)
        llm_id = str(body.get("llm_id", ""))
        router_any: Any = service.router
        resolved: Any | None = router_any.backend_for(llm_id) if router_any is not None else None
        if resolved is None:
            raise HTTPException(status_code=404, detail=service.t("llm.not_found", llm_id=llm_id))
        backend: Any = resolved[0]
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            completion = await asyncio.wait_for(
                backend.chat([{"role": "user", "content": "ping"}], temperature=0.0),
                timeout=30.0,
            )
        except NotImplementedError:
            raise
        except Exception as exc:
            message = str(exc)
            for cfg in getattr(service.router, "_configs", {}).values():
                key = getattr(cfg, "api_key", "")
                if key and key in message:
                    message = message.replace(key, "***")
            raise HTTPException(status_code=502, detail=message[:300]) from exc
        return {
            "ok": True,
            "seconds": round(loop.time() - started, 2),
            "reply": completion.text[:200],
        }

    # ------------------------------------------------------------- websocket

    @app.get("/logs")
    async def logs(_: None = Depends(guard)) -> Any:
        require_admin(service.t)
        return feed.history()

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket, _: None = Depends(guard)) -> None:
        await websocket.accept()
        feed = getattr(app.state, "log_feed", None)
        queue: asyncio.Queue[Any] = asyncio.Queue()
        unsubscribe = service.on("*", _ws_relay(queue))
        logs_enabled = False
        tasks: set[asyncio.Task[Any]] = set()
        try:
            while True:
                # whichever fires first: a queued relay/log line or a client frame
                get_task = asyncio.create_task(queue.get())
                recv_task = asyncio.create_task(websocket.receive_text())
                tasks = {get_task, recv_task}
                done, pending = await asyncio.wait(
                    {get_task, recv_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if recv_task in done:
                    try:
                        frame = json.loads(recv_task.result() or "{}")
                    except json.JSONDecodeError:
                        frame = {}
                    if (
                        isinstance(frame, dict)
                        and frame.get("type") == "logs"
                        and feed is not None
                        and not logs_enabled
                    ):
                        try:
                            require_admin(service.t)
                        except AccessDenied as exc:
                            await websocket.send_json(
                                event_frame("error", {"code": exc.code, "message": str(exc)})
                            )
                        else:
                            history = feed.subscribe(queue)
                            logs_enabled = True
                            for historic in history:
                                await websocket.send_json(historic)
                if get_task in done:
                    await websocket.send_json(get_task.result())
        except WebSocketDisconnect:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            unsubscribe()
            if feed is not None:
                feed.unsubscribe(queue)

    def _ws_relay(queue: asyncio.Queue[Any]) -> Any:
        async def relay(event: str, **payload: Any) -> None:
            await queue.put(event_frame(event, payload))

        return relay

    return app


__all__ = ["create_app"]
