"""Portable attachment intake for authenticated platform adapters."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
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

logger = logging.getLogger("mailflow.chat_files")

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
    content_type = content_type.partition(";")[0].strip().casefold()
    if not content_type or content_type == "application/octet-stream":
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        for magic, image_type in (
            (b"\x89PNG", "image/png"),
            (b"\xff\xd8\xff", "image/jpeg"),
            (b"GIF8", "image/gif"),
            (b"%PDF", "application/pdf"),
        ):
            if data.startswith(magic):
                content_type = image_type
                break
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


def framework_has_files(parts: Any) -> bool:
    """Check message structure without asking the platform to download media."""
    for part in cast(list[Any], parts or []):
        raw_kind = getattr(part, "type", type(part).__name__)
        kind = str(getattr(raw_kind, "value", raw_kind)).casefold()
        if kind in {"file", "image"}:
            return True
    return False


async def framework_quote(parts: Any, bot: Any = None) -> dict[str, str] | None:
    """Normalize a framework reply segment into text and its platform message ID."""
    for part in cast(list[Any], parts or []):
        raw_kind = getattr(part, "type", type(part).__name__)
        kind = str(getattr(raw_kind, "value", raw_kind)).casefold()
        if "reply" not in kind and "quote" not in kind:
            continue
        data = getattr(part, "data", {}) or {}
        message_id = str(data.get("id") or data.get("message_id") or getattr(part, "id", "") or "")
        sender_id = str(data.get("sender_id") or getattr(part, "sender_id", "") or "")
        quoted = (
            data.get("message")
            or data.get("content")
            or getattr(part, "chain", None)
            or getattr(part, "message", None)
            or getattr(part, "message_str", "")
        )
        if isinstance(quoted, str) and quoted.strip():
            return {"message_id": message_id, "text": quoted, "sender_id": sender_id}
        chunks: list[str] = []
        filename = ""
        for item in cast(list[Any], quoted or []):
            if isinstance(item, dict):
                mapping = cast(dict[str, Any], item)
                raw_type: Any = mapping.get("type", "")
                item_data: dict[str, Any] = mapping.get("data", {})
                fallback_text = fallback_name = ""
            else:
                raw_type = getattr(item, "type", type(item).__name__)
                item_data = getattr(item, "data", {}) or {}
                fallback_text = str(getattr(item, "text", "") or "")
                fallback_name = str(getattr(item, "name", "") or "")
            item_kind = str(getattr(raw_type, "value", raw_type)).casefold()
            if item_kind == "text" or "plain" in item_kind:
                chunks.append(str(item_data.get("text") or fallback_text))
            if item_kind in {"file", "image"}:
                filename = str(item_data.get("name") or fallback_name)
        text = "".join(chunks)
        if not text and bot is not None and message_id:
            try:
                result: dict[str, Any] = await bot.call_api("get_msg", message_id=int(message_id))
                message: dict[str, Any] = result.get("data", result)
                original_sender: dict[str, Any] = message.get("sender") or {}
                sender_id = str(original_sender.get("user_id") or sender_id)
                segments: list[dict[str, Any]] = message.get("message", [])
                if isinstance(segments, str):
                    return {"message_id": message_id, "text": segments, "sender_id": sender_id}
                text = "".join(
                    str(item.get("data", {}).get("text") or "")
                    for item in segments
                    if item.get("type") == "text"
                )
                filename = next(
                    (
                        str(item.get("data", {}).get("name") or "")
                        for item in segments
                        if item.get("type") in {"file", "image"}
                    ),
                    filename,
                )
            except Exception:
                logger.debug("quoted framework message is unavailable", exc_info=True)
        return {
            "message_id": message_id,
            "text": text.strip(),
            "filename": filename,
            "sender_id": sender_id,
        }
    return None


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


async def receive_files(service: MailFlowService, payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("files")
    if not raw and not payload.get("file_error"):
        payload["attachment_refs"] = []
        return []
    if not await service.chat_message_allowed(
        text=str(payload.get("text") or ""),
        sender=str(payload.get("sender") or ""),
        provider=str(payload.get("provider") or ""),
        instance_id=str(payload.get("instance_id") or ""),
        chat_id=str(payload.get("chat_id") or ""),
        chat_type=str(payload.get("chat_type") or ""),
        mentioned=payload.get("mentioned") is True,
        has_files=True,
    ):
        payload["attachment_refs"] = []
        return []
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
            references: list[dict[str, Any]] = []
            for row in rows:
                reference = await service.workspace_call("upload", row)
                references.append(reference)
            if request_id:
                await service.storage.set_preference(key, json.dumps(references))
        elif done == "1":
            # The old flag has no references. Upload itself deduplicates bytes,
            # so rebuild usable metadata without creating a second file.
            references = [await service.workspace_call("upload", row) for row in rows]
        else:
            try:
                references = cast(list[dict[str, Any]], json.loads(done))
            except (ValueError, TypeError):
                references = []
        payload["attachment_refs"] = references
        return references
