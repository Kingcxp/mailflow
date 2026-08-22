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
from typing import TYPE_CHECKING, Any, cast

from fastapi import (  # pyright: ignore[reportMissingTypeStubs]
    Depends,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse  # pyright: ignore[reportMissingTypeStubs]

from mailflow_server.auth import check_basic_auth, require_credentials

logger = logging.getLogger("mailflow.server")

if TYPE_CHECKING:
    from mailflow.service import MailFlowService

_LOG_BUFFER = 500


class _LogFeed(logging.Handler):
    """Fan-out handler feeding connected websocket clients."""

    def __init__(self) -> None:
        super().__init__()
        self.recent: deque[str] = deque(maxlen=_LOG_BUFFER)
        self.queues: list[asyncio.Queue[str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        line = f"{record.levelname} {record.name}: {record.getMessage()}"
        self.recent.append(line)
        for queue in list(self.queues):
            with contextlib.suppress(Exception):
                queue.put_nowait(line)


def _jsonable(payload: Any) -> Any:
    """Best-effort conversion of pydantic models/datetimes for JSON."""
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    if isinstance(payload, (list, tuple)):
        return [_jsonable(item) for item in payload]
    return payload


def create_app(service: MailFlowService) -> FastAPI:
    app = FastAPI(title="MailFlow server", version="0.1.0")
    username, password = require_credentials(service.config.server)

    def guard(request: Request) -> None:
        check_basic_auth(request, username, password)

    # ------------------------------------------------------------------ REST

    @app.get("/snapshot")
    def snapshot(_: None = Depends(guard)) -> Any:
        return _jsonable(service.snapshot())

    @app.get("/mails")
    def mails(limit: int | None = None, _: None = Depends(guard)) -> Any:
        return JSONResponse(_jsonable(asyncio.run(service.list_mails(limit=limit))))

    @app.get("/mails/{record_id}")
    async def get_mail(record_id: str, _: None = Depends(guard)) -> Any:
        record = await service.get_mail(record_id)
        if record is None:
            raise HTTPException(status_code=404, detail="mail not found")
        return _jsonable(record)

    @app.post("/mails/{record_id}/urgency")
    async def set_urgency(record_id: str, body: dict[str, Any], _: None = Depends(guard)) -> Any:
        from mailflow.domain import parse_urgency

        level = str(body.get("urgency", ""))
        urgency = None if level == "auto" else parse_urgency(level)
        record = await service.set_mail_urgency(record_id, urgency)
        if record is None:
            raise HTTPException(status_code=404, detail="mail not found")
        return _jsonable(record)

    @app.delete("/mails/{record_id}")
    async def delete_mail(record_id: str, _: None = Depends(guard)) -> Any:
        if not await service.delete_mail(record_id):
            raise HTTPException(status_code=404, detail="mail not found")
        return {"ok": True}

    @app.get("/actions")
    async def actions(_: None = Depends(guard)) -> Any:
        return _jsonable(await service.list_actions())

    @app.get("/trash")
    async def trash(_: None = Depends(guard)) -> Any:
        return _jsonable(await service.list_trash())

    @app.post("/trash/{record_id}/restore")
    async def restore(record_id: str, _: None = Depends(guard)) -> Any:
        record = await service.restore_mail(record_id)
        if record is None:
            raise HTTPException(status_code=404, detail="trash record not found")
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

    @app.post("/plugins/{plugin_id}/enable")
    async def enable_plugin(plugin_id: str, _: None = Depends(guard)) -> Any:
        created = await service.plugin_enable(plugin_id)
        return {"ok": True, "created_instance": created}

    @app.post("/plugins/{plugin_id}/disable")
    async def disable_plugin(plugin_id: str, _: None = Depends(guard)) -> Any:
        await service.plugin_disable(plugin_id)
        return {"ok": True}

    @app.post("/llms/test")
    async def test_llm(body: dict[str, Any], _: None = Depends(guard)) -> Any:
        llm_id = str(body.get("llm_id", ""))
        router_any: Any = service.router
        resolved: Any | None = router_any.backend_for(llm_id) if router_any is not None else None
        if resolved is None:
            raise HTTPException(status_code=404, detail=f"llm {llm_id!r} is not configured")
        backend: Any = resolved[0]
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            completion = await asyncio.wait_for(
                backend.chat([{"role": "user", "content": "ping"}], temperature=0.0),
                timeout=30.0,
            )
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

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        request = cast(Any, websocket)
        try:
            check_basic_auth(request, username, password)
        except HTTPException:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        feed = getattr(app.state, "log_feed", None)
        queue: asyncio.Queue[Any] = asyncio.Queue()
        unsubscribe = service.on("*", _ws_relay(queue))
        logs_enabled = False
        try:
            while True:
                # whichever fires first: a queued relay/log line or a client frame
                get_task = asyncio.create_task(queue.get())
                recv_task = asyncio.create_task(websocket.receive_text())
                done, pending = await asyncio.wait(
                    {get_task, recv_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                if recv_task in done:
                    frame = json.loads(recv_task.result() or "{}")
                    if frame.get("type") == "logs" and feed is not None and not logs_enabled:
                        for line in list(feed.recent):
                            await websocket.send_text(json.dumps({"event": "log", "line": line}))
                        feed.queues.append(queue)
                        logs_enabled = True
                if get_task in done:
                    await websocket.send_text(get_task.result())
        except WebSocketDisconnect:
            pass
        finally:
            unsubscribe()
            if feed is not None and queue in feed.queues:
                feed.queues.remove(queue)

    def _ws_relay(queue: asyncio.Queue[Any]) -> Any:
        async def relay(event: str, **payload: Any) -> None:
            await queue.put(json.dumps({"event": event, "payload": _jsonable(payload)}))

        return relay

    # attach the log feed once per app so /ws clients can opt in
    feed = _LogFeed()
    app.state.log_feed = feed
    runtime_logs = getattr(service, "_logging_runtime", None)
    if runtime_logs is not None:
        mailflow_logger = logging.getLogger("mailflow")
        mailflow_logger.addHandler(feed)

    return app


__all__ = ["create_app"]
