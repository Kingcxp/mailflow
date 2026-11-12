"""Opt-in source state synchronization and a journal of explicitly requested changes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from mailflow.contracts import StateSyncSource
from mailflow.domain import MailRecord, SourceMailState
from mailflow.errors import error_code
from mailflow.permissions import require_admin

if TYPE_CHECKING:
    from mailflow.service import MailFlowService


class SourceSync:
    def __init__(self, service: MailFlowService) -> None:
        self.service = service
        self.lock = asyncio.Lock()
        self.offset = 0

    def source(self, record: MailRecord) -> StateSyncSource | None:
        account = next(
            (a for a in self.service.config.accounts if a.account_id == record.mail.account_id),
            None,
        )
        source = self.service.sources.get(record.mail.account_id)
        return (
            source
            if account
            and account.enabled
            and account.state_sync
            and isinstance(source, StateSyncSource)
            else None
        )

    async def journal(self) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]], await self.service.workflows.read_value("source.operations", [])
        )

    async def save(self, rows: list[dict[str, Any]]) -> None:
        # Completed operations need no journal; location and trash provenance live separately.
        await self.service.workflows.write_value("source.operations", rows)

    async def change(self, record: MailRecord, changes: dict[str, Any]) -> dict[str, str]:
        async with self.lock:
            return await self.change_locked(record, changes)

    async def change_locked(self, record: MailRecord, changes: dict[str, Any]) -> dict[str, str]:
        """The caller holds lock, including any preceding local state change."""
        source = self.source(record)
        if source is None:
            return {"state": "disabled", "message": self.service.t("experience.source_off")}
        changes = dict(changes)
        if changes.get("deleted"):
            await self.service.workflows.write_value(
                "source.original." + record.record_id,
                record.mail.provider_message_id.rsplit(":", 2)[0],
            )
        elif changes.get("deleted") is False:
            changes["restore_folder"] = await self.service.workflows.read_value(
                "source.original." + record.record_id, ""
            )
            await self.service.storage.set_preference("source.trash." + record.record_id, "remote")
        rows = await self.journal()
        # A newer explicit state supersedes only the same fields, never a pending deletion.
        for previous in rows:
            if previous["record_id"] == record.record_id:
                previous["changes"] = {
                    k: v for k, v in previous["changes"].items() if k not in changes
                }
        rows = [r for r in rows if r["changes"]]
        row: dict[str, Any] = {
            "id": uuid4().hex,
            "record_id": record.record_id,
            "account_id": record.mail.account_id,
            "changes": changes,
            "state": "pending",
            "error_code": "",
        }
        rows.append(row)
        await self.save(rows)
        try:
            state = await asyncio.wait_for(source.change_source_state(record.mail, changes), 30)
            if not state.known or any(
                getattr(state, key, None) != value
                for key, value in changes.items()
                if key != "restore_folder"
            ):
                row["state"] = "unknown"
            else:
                await self.apply(record, state, remote_delete=bool(changes.get("deleted")))
                rows.remove(row)
        except Exception as exc:
            row.update(state="unknown", error_code=error_code(exc))
        await self.save(rows)
        result: dict[str, str] = {
            "state": row["state"] if row in rows else "synced",
            "message": self.service.t(
                "experience.source_pending" if row in rows else "experience.source_synced"
            ),
        }
        await self.service.events.emit("mail.source.changed", record_id=record.record_id, **result)
        return result

    async def apply(
        self,
        record: MailRecord,
        state: SourceMailState,
        *,
        remote_delete: bool = False,
        blocked: set[str] | None = None,
    ) -> None:
        if not state.known:
            return
        blocked = blocked or set()
        service = self.service
        async with service.runtime.action_store.lock, service.runtime.write_lock:
            active = await service.storage.get_mail(record.record_id)
            trash = (
                next(
                    (
                        t
                        for t in await service.storage.list_trash()
                        if t.record_id == record.record_id
                    ),
                    None,
                )
                if active is None
                else None
            )
            current = active or (trash.to_mail_record() if trash else None)
            if current is None:
                return
            previous_location = current.mail.provider_message_id
            if state.provider_message_id:
                current.mail.provider_message_id = state.provider_message_id
            for name in ("is_read", "starred", "archived"):
                value = getattr(state, name)
                if value is not None and name not in blocked:
                    setattr(current, name, value)
            provenance = await service.storage.get_preference("source.trash." + record.record_id)
            if state.deleted and "deleted" not in blocked:
                if provenance == "local_restore":
                    return
                if active:
                    await service.workflows.write_value(
                        "source.original." + record.record_id,
                        previous_location.rsplit(":", 2)[0],
                    )
                    await service.storage.save_mail(current)
                    await service.runtime.action_store.trash_mail(current)
                    await service.storage.set_preference(
                        "source.trash." + record.record_id, "remote"
                    )
                elif remote_delete:
                    await service.storage.set_preference(
                        "source.trash." + record.record_id, "remote"
                    )
                await service.workflows.write_value(
                    "source.location." + record.record_id, current.mail.provider_message_id
                )
            elif (
                not state.deleted and trash and provenance == "remote" and "deleted" not in blocked
            ):
                await service.storage.restore_from_trash(record.record_id)
                await service.storage.save_mail(current)
                await service.storage.set_preference("source.trash." + record.record_id, "")
                for identity in {record.record_id, current.mail.normalized_message_id()}:
                    await service.storage.set_preference("mail.deleted." + identity, "")
            elif active:
                await service.storage.save_mail(current)
                if not state.deleted and provenance == "local_restore":
                    await service.storage.set_preference("source.trash." + record.record_id, "")
        await service.events.emit("mail.state.changed", record_id=record.record_id)

    async def refresh(self, values: dict[str, Any] | None = None) -> None:
        if not any(a.enabled and a.state_sync for a in self.service.config.accounts):
            return
        async with self.lock:
            rows = await self.journal()
            records = await self.service.storage.list_mails()
            for trash in await self.service.storage.list_trash():
                if await self.service.storage.get_preference(
                    "source.trash." + trash.record_id
                ) == "remote" or any(r["record_id"] == trash.record_id for r in rows):
                    record = trash.to_mail_record()
                    location = await self.service.workflows.read_value(
                        "source.location." + record.record_id
                    )
                    if location:
                        record.mail.provider_message_id = location
                    records.append(record)
            records = [r for r in records if self.source(r) is not None]
            if not records:
                return
            selected = records[self.offset : self.offset + 50]
            self.offset = 0 if self.offset + 50 >= len(records) else self.offset + 50
            for account_id in {r.mail.account_id for r in selected}:
                batch = [r for r in selected if r.mail.account_id == account_id]
                source = self.source(batch[0])
                if source is None:
                    continue
                try:
                    states = await asyncio.wait_for(
                        source.read_source_state([r.mail for r in batch]), 30
                    )
                except Exception:
                    await self.service.events.emit(
                        "mail.source.failed",
                        account_id=account_id,
                        code="experience.source_pending",
                    )
                    continue
                for state in states:
                    found = next((r for r in batch if r.mail.message_id == state.message_id), None)
                    if found is None or not state.known:
                        continue
                    blocked: set[str] = set()
                    confirmed_delete = False
                    for row in list(rows):
                        if row["record_id"] != found.record_id:
                            continue
                        if all(
                            getattr(state, key, None) == value
                            for key, value in row["changes"].items()
                            if key != "restore_folder"
                        ):
                            confirmed_delete = confirmed_delete or bool(
                                row["changes"].get("deleted")
                            )
                            rows.remove(row)
                        else:
                            row["state"] = "retry"
                            blocked.update(row["changes"])
                    await self.apply(found, state, remote_delete=confirmed_delete, blocked=blocked)
            await self.save(rows)

    async def status(self, values: dict[str, Any]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for account in self.service.config.accounts:
            source = self.service.sources.get(account.account_id)
            capabilities: dict[str, Any] = {}
            if isinstance(source, StateSyncSource) and values.get("detect"):
                try:
                    capabilities = (
                        await asyncio.wait_for(source.source_capabilities(), 30)
                    ).model_dump()
                except Exception:
                    capabilities = {"error": self.service.t("experience.source_pending")}
            rows.append(
                {
                    "account_id": account.account_id,
                    "enabled": account.state_sync,
                    "supported": isinstance(source, StateSyncSource),
                    "capabilities": capabilities,
                }
            )
        return {"accounts": rows, "operations": await self.journal()}

    async def retry(self, values: dict[str, Any]) -> dict[str, str]:
        require_admin(self.service.t)
        await self.refresh()
        row = next((r for r in await self.journal() if r["id"] == values.get("id")), None)
        if not row:
            return {"state": "synced", "message": self.service.t("experience.source_synced")}
        record = await self.service.storage.get_mail(row["record_id"])
        if record is None:
            trash = next(
                (
                    t
                    for t in await self.service.storage.list_trash()
                    if t.record_id == row["record_id"]
                ),
                None,
            )
            record = trash.to_mail_record() if trash else None
        if record is None:
            raise self.service.workflows.error("reply.mail_not_found")
        location = await self.service.workflows.read_value("source.location." + record.record_id)
        if location:
            record.mail.provider_message_id = location
        return await self.change(record, row["changes"])
