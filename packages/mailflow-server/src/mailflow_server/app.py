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
import base64
import contextlib
import json
import logging
import secrets
import time
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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse  # pyright: ignore[reportMissingTypeStubs]
from mailflow.domain import PendingOperation
from mailflow.errors import error_info, error_text
from mailflow.mail_query import MailQuery
from mailflow.permissions import (
    AccessDenied,
    PermissionContext,
    current_context,
    permission_scope,
    require_admin,
)
from mailflow.protocol import event_frame, json_value
from mailflow.replies import ReplyError
from mailflow.service import ConfigReloadError
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

from mailflow_server.auth import check_basic_auth, require_credentials

logger = logging.getLogger("mailflow.server")

if TYPE_CHECKING:
    from mailflow.service import MailFlowService

_LOG_BUFFER = 500
_SMART_JOB_IDLE_SECONDS = 60.0


class ActionCreate(BaseModel):
    summary: str
    due_at: datetime | None = None
    reminder_enabled: bool = True
    mail_id: str = ""
    reply_task: bool = False
    action_type: str = "errand"
    due_end: datetime | None = None
    notes: str = ""
    location: str = ""
    url: str = ""
    category: str = ""
    tags: list[str] = Field(default_factory=list)
    repeat: dict[str, Any] | None = None
    image_indices: list[int] = Field(default_factory=list)


class ActionEdit(BaseModel):
    summary: str | None = None
    due_at: datetime | None = None
    action_type: str | None = None
    due_end: datetime | None = None
    clear_end: bool = False
    notes: str | None = None
    location: str | None = None
    url: str | None = None
    clear_due: bool = False
    completed: bool | None = None
    reminder_enabled: bool | None = None
    reply_task: bool | None = None
    snoozed_until: datetime | None = None
    clear_snooze: bool = False
    category: str | None = None
    tags: list[str] | None = None
    image_indices: list[int] | None = None


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
    jobs: dict[str, dict[str, Any]] = {}

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
            tasks = [job["task"] for job in jobs.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            jobs.clear()

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
                    "params": {},
                    "message": service.t("permission.unauthorized"),
                },
                headers={"WWW-Authenticate": "Basic"},
            ) from None
        return PermissionContext(sender=username, provider="server", is_admin=True)

    identity_dependency = Depends(authenticate)

    async def guard(context: PermissionContext = identity_dependency) -> AsyncIterator[None]:
        if service.restoring:
            raise HTTPException(
                status_code=503,
                detail={"code": "ux.restore_running", "message": service.t("ux.restore_running")},
            )
        with permission_scope(context):
            yield

    # Hosts may replace identity resolution; the shared access policy still runs.
    app.state.authenticate = authenticate

    # ------------------------------------------------------------------ REST

    def http_failure(status: int, code: str, **params: Any) -> HTTPException:
        return HTTPException(
            status_code=status,
            detail={"code": code, "params": params, "message": service.t(code, **params)},
        )

    async def operation_failure(_: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, PermissionError):
            status = 403
        elif isinstance(exc, NotImplementedError):
            status = 501
        elif isinstance(exc, KeyError):
            status = 404
        elif isinstance(exc, (ValueError, ReplyError)):
            status = 400
        elif isinstance(exc, ConfigReloadError):
            status = 409
        elif isinstance(exc, TimeoutError):
            status = 504
        else:
            status = 500
        if status >= 500:
            logger.error("remote operation failed", exc_info=exc)
        return JSONResponse(status_code=status, content={"detail": error_info(exc, service.t)})

    for kind in (
        AccessDenied,
        PermissionError,
        ValueError,
        KeyError,
        NotImplementedError,
        ReplyError,
        RuntimeError,
        OSError,
        Exception,
    ):
        app.add_exception_handler(kind, operation_failure)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "errors.invalid_value",
                    "params": {},
                    "message": service.t("errors.invalid_value"),
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
                    "params": {},
                    "message": service.t("reply.confirm_invalid"),
                },
            ) from None
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=error_info(exc, service.t)) from None

    @app.post("/mails/{record_id}/replies")
    async def create_reply(record_id: str, resume: bool = False, _: None = Depends(guard)) -> Any:
        return await reply_result(service.create_reply(record_id, resume=resume))

    @app.get("/drafts/{draft_id}")
    async def get_draft(draft_id: str, _: None = Depends(guard)) -> Any:
        draft = await service.get_draft(draft_id)
        if draft is None:
            raise http_failure(404, "reply.draft_not_found", draft_id=draft_id)
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

    def query_job(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None or job["owner"] != current_context():
            raise http_failure(404, "ux.query_missing")
        return job

    @app.post("/smart/jobs")
    async def start_smart_job(body: dict[str, Any], _: None = Depends(guard)) -> Any:
        for key in list(jobs):
            if jobs[key]["state"] != "running" and time.monotonic() - jobs[key]["created"] > 1200:
                jobs.pop(key)
        if sum(job["state"] == "running" for job in jobs.values()) >= 8:
            raise http_failure(409, "ux.query_busy")
        job_id = secrets.token_urlsafe(24)
        job: dict[str, Any] = {
            "owner": current_context(),
            "created": time.monotonic(),
            "last_seen": time.monotonic(),
            "state": "running",
            "events": [],
            "seq": 0,
        }

        def progress(stage: str, done: int, total: int, detail: Any) -> None:
            job["seq"] += 1
            job["events"].append(
                {
                    "seq": job["seq"],
                    "stage": stage,
                    "done": done,
                    "total": total,
                    "detail": _jsonable(detail),
                }
            )
            job["events"] = job["events"][-300:]

        async def run() -> None:
            operation = asyncio.create_task(
                service.smart_action(
                    str(body.get("instruction", "")),
                    record_ids=body.get("record_ids"),
                    account_id=str(body.get("account_id", "")),
                    conversation=body.get("conversation"),
                    action_ids=body.get("action_ids"),
                    progress=progress,
                ),
                name=f"smart-operation-{job_id}",
            )
            try:
                while not operation.done():
                    remaining = _SMART_JOB_IDLE_SECONDS - (time.monotonic() - job["last_seen"])
                    if remaining <= 0:
                        raise asyncio.CancelledError
                    await asyncio.wait({operation}, timeout=remaining)
                job["result"] = operation.result()
                job["state"] = "done"
            except asyncio.CancelledError:
                job["state"] = "cancelled"
                raise
            except Exception as exc:
                logger.warning("smart query failed: %s", exc)
                job["state"], job["error"] = "failed", error_text(exc, service.t)
            finally:
                if not operation.done():
                    operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)

        job["task"] = asyncio.create_task(run(), name=f"smart-{job_id}")
        jobs[job_id] = job
        return {"job_id": job_id}

    @app.get("/profile")
    async def profile(_: None = Depends(guard)) -> Any:
        return {
            "profile": await service.user_profile(),
            "guidelines": await service.feedback_guidelines(),
        }

    @app.put("/profile")
    async def save_profile(body: dict[str, Any], _: None = Depends(guard)) -> Any:
        require_admin(service.t)
        if "profile" in body:
            await service.set_user_profile(str(body["profile"]))
        if "guidelines" in body:
            await service.set_feedback_guidelines(str(body["guidelines"]))
        return await profile()

    @app.post("/mails/{record_id}/chat")
    async def mail_chat(record_id: str, body: dict[str, Any], _: None = Depends(guard)) -> Any:
        return await service.chat_about_mail(record_id, body.get("messages", []))

    @app.post("/mails/{record_id}/correction")
    async def correct_mail(record_id: str, body: dict[str, Any], _: None = Depends(guard)) -> Any:
        return _jsonable(
            await service.apply_mail_correction(
                record_id,
                body.get("corrections", {}),
                note=str(body.get("note", "")),
                remember=bool(body.get("remember", False)),
            )
        )

    @app.get("/deliveries/failed")
    async def failed_deliveries(_: None = Depends(guard)) -> Any:
        return await service.failed_deliveries()

    @app.post("/deliveries/retry")
    async def retry_deliveries(body: dict[str, Any], _: None = Depends(guard)) -> Any:
        return {"delivered": await service.retry_deliveries(body.get("delivery_ids", []))}

    @app.get("/smart/jobs/{job_id}")
    async def smart_job(job_id: str, after: int = 0, _: None = Depends(guard)) -> Any:
        job = query_job(job_id)
        job["last_seen"] = time.monotonic()
        return _jsonable(
            {
                "state": job["state"],
                "cursor": job["seq"],
                "events": [event for event in job["events"] if event["seq"] > after],
                "result": job.get("result"),
                "error": job.get("error", ""),
            }
        )

    @app.delete("/smart/jobs/{job_id}")
    async def cancel_smart_job(job_id: str, _: None = Depends(guard)) -> Any:
        job = query_job(job_id)
        job["task"].cancel()
        await asyncio.gather(job["task"], return_exceptions=True)
        jobs.pop(job_id, None)
        return {"ok": True}

    @app.post("/smart/plans/{plan_id}")
    async def apply_smart_plan(plan_id: str, body: dict[str, Any], _: None = Depends(guard)) -> Any:
        return _jsonable(
            await service.apply_smart_plan(
                plan_id,
                [PendingOperation.model_validate(row) for row in body.get("operations", [])],
            )
        )

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

    @app.post("/mails/query")
    async def query_mails(body: MailQuery, _: None = Depends(guard)) -> Any:
        return _jsonable(await service.query_mails(body))

    @app.get("/mails/count")
    async def count_mails(_: None = Depends(guard)) -> Any:
        return {"count": await service.count_mails()}

    @app.post("/analysis/pause")
    async def pause_analysis(body: dict[str, Any], _: None = Depends(guard)) -> Any:
        await service.set_analysis_paused(bool(body.get("paused", True)))
        return _jsonable(service.snapshot())

    @app.post("/mails/batch")
    async def batch_mail(body: dict[str, Any], _: None = Depends(guard)) -> Any:
        return _jsonable(
            await service.batch_mail(
                body.get("record_ids", []),
                operation=body.get("operation", "delete"),
                delete_actions=bool(body.get("delete_actions", False)),
            )
        )

    @app.post("/mails/state")
    async def mail_state(body: dict[str, Any], _: None = Depends(guard)) -> Any:
        return _jsonable(
            await service.set_mail_state(
                body.get("record_ids", []),
                **{key: body[key] for key in ("is_read", "starred", "archived") if key in body},
            )
        )

    @app.get("/mails/{record_id}/attachments/{index}")
    async def attachment(record_id: str, index: int, _: None = Depends(guard)) -> Any:
        return {
            "data": base64.b64encode(await service.fetch_attachment(record_id, index)).decode(
                "ascii"
            )
        }

    @app.get("/drafts")
    async def drafts(state: str = "", _: None = Depends(guard)) -> Any:
        return _jsonable(await service.list_drafts(state=state))

    @app.get("/cleanup/preview")
    async def cleanup_preview(_: None = Depends(guard)) -> Any:
        require_admin(service.t)
        return _jsonable(await service.cleanup_preview())

    @app.post("/cleanup/selected")
    async def cleanup_selected(body: dict[str, Any], _: None = Depends(guard)) -> Any:
        return _jsonable(await service.cleanup_selected(body.get("record_ids", [])))

    @app.post("/cleanup")
    async def cleanup(_: None = Depends(guard)) -> Any:
        moved, purged = await service.run_cleanup()
        return {"moved": moved, "purged": purged}

    @app.get("/mails/{record_id}")
    async def get_mail(record_id: str, _: None = Depends(guard)) -> Any:
        record = await service.get_mail(record_id)
        if record is None:
            raise http_failure(404, "mail.not_found", mail_id=record_id)
        return _jsonable(record)

    @app.post("/mails/{record_id}/urgency")
    async def set_urgency(record_id: str, body: dict[str, Any], _: None = Depends(guard)) -> Any:
        from mailflow.domain import parse_urgency

        level = str(body.get("urgency", ""))
        urgency = None if level == "auto" else parse_urgency(level)
        record = await service.set_mail_urgency(record_id, urgency)
        if record is None:
            raise http_failure(404, "mail.not_found", mail_id=record_id)
        return _jsonable(record)

    @app.delete("/mails/{record_id}")
    async def delete_mail(record_id: str, _: None = Depends(guard)) -> Any:
        if not await service.delete_mail(record_id):
            raise http_failure(404, "mail.not_found", mail_id=record_id)
        return {"ok": True}

    @app.post("/mails/delete")
    async def delete_mails(body: dict[str, Any], _: None = Depends(guard)) -> Any:
        return {"count": await service.delete_mails(body.get("record_ids", []))}

    @app.post("/mails/purge-expired")
    async def purge_expired(body: dict[str, Any], _: None = Depends(guard)) -> Any:
        return {"count": await service.purge_expired_mails(body.get("record_ids"))}

    @app.get("/actions")
    async def actions(
        include_completed: bool = False, preview_days: int = 0, _: None = Depends(guard)
    ) -> Any:
        return _jsonable(await service.list_actions_all(preview_days=min(30, max(0, preview_days))))

    @app.get("/action-plans")
    async def action_plans(_: None = Depends(guard)) -> Any:
        return _jsonable(await service.action_plans())

    @app.put("/action-plans/{plan_id}")
    async def update_plan(plan_id: str, body: dict[str, Any], _: None = Depends(guard)) -> Any:
        for key in ("due_at", "due_end"):
            if body.get(key):
                body[key] = datetime.fromisoformat(str(body[key]))
        return _jsonable(await service.update_action_plan(plan_id, **body))

    @app.delete("/action-plans/{plan_id}")
    async def stop_plan(plan_id: str, _: None = Depends(guard)) -> Any:
        return {"ok": await service.stop_action_plan(plan_id)}

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
            raise http_failure(404, "trash.not_found", record_id=record_id)
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
        spec = await service.set_setting(key, body.get("value"))
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
            raise http_failure(404, "llm.not_found", llm_id=llm_id)
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
            raise HTTPException(status_code=502, detail=error_info(exc, service.t)) from exc
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
                                event_frame("error", error_info(exc, service.t))
                            )
                        else:
                            history = feed.subscribe(queue)
                            logs_enabled = True
                            for historic in history:
                                await websocket.send_json(historic)
                if get_task in done:
                    await websocket.send_json(get_task.result())
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            # Release subscriptions before awaiting children: server shutdown
            # may cancel the handler again while those tasks are settling.
            unsubscribe()
            if feed is not None:
                feed.unsubscribe(queue)
            for task in tasks:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, return_exceptions=True)

    def _ws_relay(queue: asyncio.Queue[Any]) -> Any:
        async def relay(event: str, **payload: Any) -> None:
            await queue.put(event_frame(event, payload))

        return relay

    return app


__all__ = ["create_app"]
