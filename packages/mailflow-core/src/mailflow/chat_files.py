"""Portable attachment intake for authenticated platform adapters."""

from __future__ import annotations

import asyncio
import base64
import inspect
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx

from mailflow.domain import MAX_ATTACHMENT_BYTES
from mailflow.errors import translated_error
from mailflow.permissions import permission_scope, require_admin

if TYPE_CHECKING:
    from mailflow.service import MailFlowService


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
        kind = str(getattr(part, "type", type(part).__name__)).casefold()
        if kind not in {"file", "image"}:
            continue
        data = getattr(part, "data", {}) or {}
        name = str(data.get("name") or getattr(part, "name", "") or "attachment")
        getter = getattr(part, "get_file", None)
        if getter is not None:
            resolved = getter()
            path = await resolved if inspect.isawaitable(resolved) else resolved

            def read(selected_path: Any = path) -> bytes:
                with Path(str(selected_path)).open("rb") as handle:
                    return handle.read(MAX_ATTACHMENT_BYTES + 1)

            content = await asyncio.to_thread(read)
        else:
            url = str(data.get("url") or "")
            if not url and bot is not None:
                result = await bot.get_file(file_id=data.get("file_id") or data.get("file"))
                url = str(result.get("url") or "")
            content = await download_file(url)
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment exceeds the supported size")
        files.append(
            encoded_file(name, content, mimetypes.guess_type(name)[0] or "application/octet-stream")
        )
    return files


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
