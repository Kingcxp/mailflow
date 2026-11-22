"""Portable configuration and consistent storage backups, with rollback on restore."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import tomllib
import zipfile
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from mailflow import __version__
from mailflow.config import MailFlowConfig, config_text, parse_config, write_config
from mailflow.domain import utcnow
from mailflow.errors import translated_error
from mailflow.lifecycle import complete_io
from mailflow.migrations import CONFIG_VERSION, DATABASE_VERSION, DataVersionError, check_version

if TYPE_CHECKING:
    from mailflow.service import MailFlowService


def _pack(configuration: str, database: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format": 1,
                    "version": __version__,
                    "created_at": utcnow().isoformat(),
                    "config_version": CONFIG_VERSION,
                    "database_version": DATABASE_VERSION,
                }
            ),
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
                path.chmod(0o600)
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
    payload = await asyncio.to_thread(_pack, configuration, database)
    await asyncio.to_thread(_write, path, payload, overwrite=overwrite)
    return str(path)


async def export_backup(service: MailFlowService) -> bytes:
    async with service.runtime.action_store.lock, service.runtime.write_lock:
        database: bytes = await _capability(service, "export_backup")()
        configuration = config_text(service.config)
    return await asyncio.to_thread(_pack, configuration, database)


def _unpack(payload: bytes) -> tuple[MailFlowConfig, bytes, dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest["format"] != 1:
            raise ValueError("unknown backup format")
        check_version(manifest.get("config_version", 0), "config")
        check_version(manifest.get("database_version", 0), "database")
        updated = parse_config(tomllib.loads(archive.read("config.toml").decode("utf-8-sig")))
        return updated, archive.read("mailflow.sqlite"), manifest


async def preview_backup(service: MailFlowService, payload: bytes) -> dict[str, Any]:
    try:
        updated, database, manifest = await asyncio.to_thread(_unpack, payload)
        counts: dict[str, Any] = await _capability(service, "inspect_backup")(database)
    except DataVersionError:
        raise
    except Exception as exc:
        raise translated_error(ValueError, service.t, "ux.backup_invalid") from exc
    return {
        **counts,
        "accounts": [a.account_id for a in updated.accounts],
        "created_at": manifest.get("created_at", ""),
        "config_version": updated.source_version,
        "database_version": counts.get("database_version", manifest.get("database_version", 0)),
    }


async def preview_config_import(
    service: MailFlowService, configuration: bytes, database: bytes | None = None
) -> dict[str, Any]:
    updated = parse_config(tomllib.loads(configuration.decode("utf-8-sig")))
    counts: dict[str, Any] = {}
    if database is not None:
        counts = await _capability(service, "inspect_backup")(database)
    return {
        "config_version": updated.source_version,
        "target_version": CONFIG_VERSION,
        "target_database_version": DATABASE_VERSION,
        "accounts": [a.account_id for a in updated.accounts],
        "llms": len(updated.llms),
        "notifiers": len(updated.notifiers),
        "includes_database": database is not None,
        **counts,
    }


def _keep_local_sections(
    updated: MailFlowConfig, current: MailFlowConfig, sections: tuple[str, ...]
) -> None:
    for section in sections:
        setattr(updated, section, getattr(current, section).model_copy(deep=True))
    updated.env_placeholders = {
        path: value for path, value in updated.env_placeholders.items() if path[0] not in sections
    }
    updated.env_placeholders.update(
        (path, value) for path, value in current.env_placeholders.items() if path[0] in sections
    )


async def import_config(
    service: MailFlowService, configuration: bytes, database: bytes | None = None
) -> str:
    """Reuse confirmed restore and rollback; never adopt another installation's paths."""
    if service.config_path is None:
        raise translated_error(ValueError, service.t, "errors.config_required")
    await preview_config_import(service, configuration, database)
    updated = parse_config(tomllib.loads(configuration.decode("utf-8-sig")))
    # Keep the current server reachable while its settings are being imported.
    _keep_local_sections(updated, service.config, ("storage", "server"))
    directory = service.config_path.parent / "backups"
    recovery = await _apply_restore(service, updated, database, directory / "import.zip")
    await service.events.emit("config.changed", key="import", keys=("import",))
    return recovery


async def run_auto_backup(service: MailFlowService, now: datetime | None = None) -> str:
    config = service.config.general
    if not config.auto_backup:
        return ""
    local = (now or datetime.now(UTC)).astimezone(ZoneInfo(config.timezone))
    day = local.date() - timedelta(days=int(local.time() < time.fromisoformat(config.backup_time)))
    async with service._backup_lock:  # pyright: ignore[reportPrivateUsage]
        if await service.storage.get_preference("backup.last_auto") == day.isoformat():
            return ""
        directory = await asyncio.to_thread(lambda: Path(config.backup_directory).expanduser())
        if not directory.is_absolute():
            directory = (
                service.config_path.parent if service.config_path else Path.cwd()
            ) / directory
        path = directory / f"auto-{day.isoformat()}-{uuid4().hex[:8]}.zip"
        result = await create_backup(service, str(path))
        await service.storage.set_preference("backup.last_auto", day.isoformat())

        def prune() -> None:
            for old in sorted(
                directory.glob("auto-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True
            )[config.backup_keep :]:
                old.unlink()

        await asyncio.to_thread(prune)
        await service.events.emit("backup.created", path=result)
        return result


async def restore_backup(
    service: MailFlowService, source: str, *, payload: bytes | None = None
) -> str:
    path = await asyncio.to_thread(lambda: Path(source).expanduser().resolve())
    validate = _capability(service, "validate_backup")
    try:
        payload = payload if payload is not None else await asyncio.to_thread(path.read_bytes)
        updated, database, _ = await asyncio.to_thread(_unpack, payload)
        await validate(database)
    except DataVersionError:
        raise
    except Exception as exc:
        raise translated_error(ValueError, service.t, "ux.backup_invalid") from exc
    # A portable backup must restore to this installation's database, not to
    # the absolute path of the computer on which it was created.
    _keep_local_sections(updated, service.config, ("storage",))
    return await _apply_restore(service, updated, database, path)


async def _apply_restore(
    service: MailFlowService, updated: MailFlowConfig, database: bytes | None, path: Path
) -> str:
    restore = _capability(service, "restore_backup")
    recovery_path = path.with_name(f"before-restore-{utcnow():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}.zip")
    previous = service.config
    previous_database: bytes | None = None
    previous_file: bytes | None = None
    applied = False
    was_started = service.started
    service.restoring = True
    try:
        async with service._lifecycle_lock:  # pyright: ignore[reportPrivateUsage]
            await service._stop_unlocked(persist_running=True)  # pyright: ignore[reportPrivateUsage]
            await service.storage.initialize()
            service._resources_closed = False  # pyright: ignore[reportPrivateUsage]
            # Quiesce receivers before snapshotting: config-only import must not
            # replace a database snapshot taken while new mail was still arriving.
            snapshot: bytes = await _capability(service, "export_backup")()
            previous_database = snapshot
            if service.config_path is not None and await asyncio.to_thread(
                service.config_path.is_file
            ):
                previous_file = await asyncio.to_thread(service.config_path.read_bytes)
            recovery = await asyncio.to_thread(_pack, config_text(previous), snapshot)
            await complete_io(_write, recovery_path, recovery, overwrite=False)
            applied = True
            if database is not None:
                await restore(database)
            await service.runtime.reset_after_restore()
            service._smart_plans.clear()  # pyright: ignore[reportPrivateUsage]
            service._smart_history.clear()  # pyright: ignore[reportPrivateUsage]
            service.config = updated
            if service.config_path is not None:
                await complete_io(write_config, updated, service.config_path)
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
            if applied and previous_database is not None:
                await restore(previous_database)
            await service.runtime.reset_after_restore()
            service.config = previous
            if applied and service.config_path is not None:
                if previous_file is not None:
                    await complete_io(_write, service.config_path, previous_file, overwrite=True)
                else:
                    await asyncio.to_thread(service.config_path.unlink, missing_ok=True)
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
