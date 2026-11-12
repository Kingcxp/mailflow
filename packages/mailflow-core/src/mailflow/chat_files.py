"""Portable attachment intake for authenticated platform adapters."""

from __future__ import annotations

import asyncio
import base64
import inspect
import mimetypes
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx

from mailflow.domain import MAX_ATTACHMENT_BYTES
from mailflow.errors import translated_error
from mailflow.permissions import current_context, permission_scope, require_admin

if TYPE_CHECKING:
    from mailflow.service import MailFlowService


FileSender = Callable[[dict[str, Any]], Awaitable[None]]
_file_sender: ContextVar[FileSender | None] = ContextVar("mailflow_file_sender", default=None)


@contextmanager
def file_sender_scope(sender: FileSender) -> Generator[None]:
    token = _file_sender.set(sender)
    try:
        yield
    finally:
        _file_sender.reset(token)


async def send_file(service: MailFlowService, values: dict[str, Any]) -> bool:
    return await deliver_file(service, await service.workflows.file_download(values))


async def deliver_file(service: MailFlowService, file: dict[str, Any]) -> bool:
    """Send a verified managed file or an in-memory export without storing a second copy."""
    from mailflow.contracts import RichNotifier
    from mailflow.domain import NotificationImage

    require_admin(service.t)
    identity = current_context()
    await service.pace_bot(identity.provider, identity.instance_id)
    sender = _file_sender.get()
    if sender:
        await sender(file)
        return True
    config = service._chat_notifier(identity.provider, identity.instance_id)  # pyright: ignore[reportPrivateUsage]
    notifier = next(
        (
            n
            for n, c in service.runtime.notification_channels()
            if config and c.notifier_id == config.notifier_id
        ),
        None,
    )
    operation = getattr(notifier, "push_file_to_target", None)
    if (
        not config
        or not isinstance(notifier, RichNotifier)
        or not notifier.capabilities.files
        or not callable(operation)
    ):
        raise translated_error(NotImplementedError, service.t, "experience.file_send_unsupported")
    kind = "user" if identity.chat_id == identity.sender else "group"
    target = kind + ":" + identity.chat_id
    await cast(Callable[[str, NotificationImage], Awaitable[None]], operation)(
        target,
        NotificationImage(
            filename=file["filename"],
            content_type=file["content_type"],
            data=base64.b64decode(file["data"]),
        ),
    )
    return True


async def framework_send_file(
    file: dict[str, Any],
    *,
    bot: Any = None,
    event: Any = None,
    group_id: str = "",
    sender: str = "",
) -> None:
    """Adapters get only a temporary copy of the already-authorized managed file."""
    import tempfile

    if bot is not None:
        operation = "upload_group_file" if group_id else "upload_private_file"
        target = {"group_id": int(group_id)} if group_id else {"user_id": int(sender)}
        result = await bot.call_api(
            operation, **target, file="base64://" + str(file["data"]), name=file["filename"]
        )
        if result:
            from mailflow.notifications import check_result

            check_result(result)
        return

    with tempfile.TemporaryDirectory(prefix="mailflow-file-") as directory:
        path = Path(directory) / str(file["filename"]).replace("\\", "/").split("/")[-1]
        await asyncio.to_thread(path.write_bytes, base64.b64decode(file["data"]))
        if event is not None:
            from importlib import import_module

            components = import_module("astrbot.api.message_components")
            result = await event.send(
                event.chain_result([components.File(name=path.name, file=str(path))])
            )
            if result is False:
                from mailflow.notifications import DeliveryError

                raise DeliveryError("rejected")


async def download_file(url: str, *, headers: dict[str, str] | None = None) -> bytes:
    """Read a platform-supplied URL without unbounded buffering or redirects."""
    data = bytearray()
    async with (
        httpx.AsyncClient(timeout=30.0) as client,
        client.stream("GET", url, headers=headers) as response,
    ):
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > MAX_ATTACHMENT_BYTES:
                raise ValueError("attachment exceeds the supported size")
    return bytes(data)


def encoded_file(
    name: str, data: bytes, content_type: str = "application/octet-stream"
) -> dict[str, str]:
    return {
        "filename": name,
        "content_type": content_type,
        "data": base64.b64encode(data).decode("ascii"),
    }


async def framework_files(parts: Any, bot: Any = None) -> list[dict[str, str]]:
    """Use the framework's resolved media, never a path supplied by the model."""
    files: list[dict[str, str]] = []
    for part in parts:
        raw_kind = getattr(part, "type", type(part).__name__)
        kind = str(getattr(raw_kind, "value", raw_kind)).casefold()
        if kind not in {"file", "image"}:
            continue
        data = getattr(part, "data", {}) or {}
        name = str(data.get("name") or getattr(part, "name", "") or "attachment")
        getter = getattr(part, "get_file", None) or getattr(part, "convert_to_file_path", None)
        if getter is not None:
            resolved = getter()
            path = await resolved if inspect.isawaitable(resolved) else resolved

            def read(selected_path: Any = path) -> bytes:
                with Path(str(selected_path)).open("rb") as handle:
                    return handle.read(MAX_ATTACHMENT_BYTES + 1)

            content = await asyncio.to_thread(read)
            if name == "attachment":
                name = Path(str(path)).name
        else:
            url = str(data.get("url") or getattr(part, "url", "") or "")
            if not url and bot is not None:
                result = await bot.call_api(
                    "get_file", file=data.get("file_id") or data.get("file")
                )
                url = str(result.get("url") or "")
            content = await download_file(url)
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment exceeds the supported size")
        files.append(
            encoded_file(name, content, mimetypes.guess_type(name)[0] or "application/octet-stream")
        )
    return files


async def framework_notice_files(event: Any, bot: Any) -> list[dict[str, str]]:
    """Resolve OneBot upload notices through the platform, not a host filesystem path."""
    file = getattr(event, "file", None)
    data: dict[str, Any] = {}
    if isinstance(file, dict):
        data = cast(dict[str, Any], file)
    elif file is not None:
        dump = getattr(file, "model_dump", None) or getattr(file, "dict", None)
        if dump:
            data = dump()
    group_id = str(getattr(event, "group_id", "") or "")
    params: dict[str, Any] = {"file_id": data.get("id") or data.get("file_id")}
    if group_id:
        params.update(group_id=int(group_id), busid=data.get("busid", 0))
    else:
        params.update(user_id=int(event.user_id))
    result = await bot.call_api(
        "get_group_file_url" if group_id else "get_private_file_url", **params
    )
    content = await download_file(str(result.get("url") or ""))
    name = str(data.get("name") or "attachment")
    return [
        encoded_file(name, content, mimetypes.guess_type(name)[0] or "application/octet-stream")
    ]


async def receive_files(service: MailFlowService, payload: dict[str, Any]) -> None:
    raw = payload.get("files")
    if not raw and not payload.get("file_error"):
        return
    context = service.permission_context(
        sender=str(payload.get("sender") or ""),
        provider=str(payload.get("provider") or ""),
        instance_id=str(payload.get("instance_id") or ""),
        chat_id=str(payload.get("chat_id") or ""),
    )
    with permission_scope(context):
        require_admin(service.t)
        if payload.get("file_error"):
            raise translated_error(ValueError, service.t, "experience.file_download_failed")
        rows = cast(list[dict[str, Any]], raw) if isinstance(raw, list) else []
        request_id = str(payload.get("request_id") or "")
        key = "file.uploads." + service.workflows_actor() + "." + request_id
        async with service.workflows.lock:
            done = await service.storage.get_preference(key) if request_id else None
        if not done:
            for row in rows:
                await service.workspace_call("upload", row)
            if request_id:
                await service.storage.set_preference(key, "1")
        if not str(payload.get("text") or "").strip():
            payload["text"] = service.command_prefix() + "mailflow reply files"
