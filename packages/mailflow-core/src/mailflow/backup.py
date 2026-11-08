"""Portable configuration and consistent storage backups, with rollback on restore."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mailflow import __version__
from mailflow.config import config_text, parse_config, write_config
from mailflow.domain import utcnow
from mailflow.errors import translated_error

if TYPE_CHECKING:
    from mailflow.service import MailFlowService


def _pack(configuration: str, database: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"format": 1, "version": __version__, "created_at": utcnow().isoformat()}),
        )
        archive.writestr("config.toml", configuration)
        archive.writestr("mailflow.sqlite", database)
    return buffer.getvalue()


def _write(path: Path, payload: bytes, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
        temporary.chmod(0o600)
        if overwrite:
            temporary.replace(path)
        else:
            # Exclusive creation also protects against a second backup racing.
            with path.open("xb") as destination:
                destination.write(payload)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _capability(service: MailFlowService, name: str) -> Any:
    operation = getattr(service.storage, name, None)
    if operation is None:
        raise translated_error(NotImplementedError, service.t, "ux.backup_unsupported")
    return operation


async def create_backup(
    service: MailFlowService, destination: str, *, overwrite: bool = False
) -> str:
    path = await asyncio.to_thread(lambda: Path(destination).expanduser().resolve())
    if path.suffix.casefold() != ".zip":
        raise translated_error(ValueError, service.t, "ux.backup_zip_required")
    async with service.runtime.action_store.lock, service.runtime.write_lock:
        database: bytes = await _capability(service, "export_backup")()
        configuration = config_text(service.config)
    await asyncio.to_thread(_write, path, _pack(configuration, database), overwrite=overwrite)
    return str(path)


async def restore_backup(service: MailFlowService, source: str) -> str:
    path = await asyncio.to_thread(lambda: Path(source).expanduser().resolve())
    validate = _capability(service, "validate_backup")
    restore = _capability(service, "restore_backup")
    try:
        payload = await asyncio.to_thread(path.read_bytes)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if json.loads(archive.read("manifest.json"))["format"] != 1:
                raise ValueError("unknown backup format")
            updated = parse_config(tomllib.loads(archive.read("config.toml").decode("utf-8")))
            database = archive.read("mailflow.sqlite")
        await validate(database)
    except Exception as exc:
        raise translated_error(ValueError, service.t, "ux.backup_invalid") from exc
    # A portable backup must restore to this installation's database, not to
    # the absolute path of the computer on which it was created.
    updated.storage = service.config.storage.model_copy(deep=True)
    recovery_path = path.with_name(f"before-restore-{utcnow():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}.zip")
    await create_backup(service, str(recovery_path))
    previous = service.config
    previous_database: bytes = await _capability(service, "export_backup")()
    previous_file = (
        service.config_path.read_bytes()
        if service.config_path and service.config_path.exists()
        else None
    )
    was_started = service.started
    service.restoring = True
    try:
        async with service._lifecycle_lock:  # pyright: ignore[reportPrivateUsage]
            await service._stop_unlocked(persist_running=True)  # pyright: ignore[reportPrivateUsage]
            await service.storage.initialize()
            service._resources_closed = False  # pyright: ignore[reportPrivateUsage]
            await restore(database)
            await service.runtime.reset_after_restore()
            service._smart_plans.clear()  # pyright: ignore[reportPrivateUsage]
            service.config = updated
            if service.config_path is not None:
                write_config(updated, service.config_path)
            await service._reload_runtime_unlocked(restart_sources=True)  # pyright: ignore[reportPrivateUsage]
            await service._load_persisted_language()  # pyright: ignore[reportPrivateUsage]
            await service.runtime.invalidate_analysis_language()
        if was_started:
            await service.start()
    except BaseException:
        async with service._lifecycle_lock:  # pyright: ignore[reportPrivateUsage]
            await service._stop_unlocked(persist_running=True)  # pyright: ignore[reportPrivateUsage]
            await service.storage.initialize()
            service._resources_closed = False  # pyright: ignore[reportPrivateUsage]
            await restore(previous_database)
            await service.runtime.reset_after_restore()
            service.config = previous
            if service.config_path is not None:
                if previous_file is not None:
                    await asyncio.to_thread(
                        _write, service.config_path, previous_file, overwrite=True
                    )
                else:
                    service.config_path.unlink(missing_ok=True)
            await service._reload_runtime_unlocked(restart_sources=True)  # pyright: ignore[reportPrivateUsage]
            await service._load_persisted_language()  # pyright: ignore[reportPrivateUsage]
            await service.runtime.invalidate_analysis_language()
        if was_started:
            with contextlib.suppress(Exception):
                await service.start()
        raise
    finally:
        service.restoring = False
    await service.events.emit("backup.restored", recovery_path=str(recovery_path))
    return str(recovery_path)
