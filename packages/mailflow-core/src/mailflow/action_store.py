"""The shared view of visible schedule entries and their storage ownership."""

from __future__ import annotations

import asyncio
import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from mailflow.contracts import StorageBackend
from mailflow.domain import (
    ActionItem,
    ActionOrigin,
    MailRecord,
    action_sort_time,
    action_time_window,
    actions_duplicate,
    to_utc,
)
from mailflow.recurrence import RecurringActions


def action_key(item: ActionItem) -> str:
    """A reanalysis-stable identity that keeps distinct simultaneous tasks apart."""
    start, end = action_time_window(item)
    return "v2|" + json.dumps(
        [
            item.mail_id,
            start.isoformat(),
            end.isoformat(),
            item.action_type,
            " ".join(item.summary.split()).casefold(),
        ],
        ensure_ascii=False,
    )


def _legacy_key(item: ActionItem) -> str:
    return f"{item.mail_id}|{action_sort_time(item).isoformat()}|{item.action_type}"


def _dismissal_key(item: ActionItem) -> str:
    return "v3|" + json.dumps(
        [action_key(item), item.notes, item.location, item.url], ensure_ascii=False
    )


def completed_key(item: ActionItem, *, legacy: bool = False) -> str:
    title = "".join(
        char
        for char in item.summary.casefold()
        if not unicodedata.category(char).startswith(("P", "Z"))
    )
    value = [item.mail_id, title, item.action_type, str(item.due_at), str(item.due_end)]
    if not legacy:
        value.extend([item.notes, item.location, item.url])
    return "done|" + hashlib.sha256(json.dumps(value).encode()).hexdigest()


def action_has_ended(item: ActionItem, now: datetime) -> bool:
    return item.due_at is not None and action_time_window(item)[1] <= to_utc(now)


@dataclass
class ScheduledAction:
    item: ActionItem
    record: MailRecord | None = None
    custom: bool = False


class ActionStore:
    def __init__(self, storage: StorageBackend) -> None:
        self.storage = storage
        self.lock = asyncio.Lock()
        self.recurring = RecurringActions(storage)

    @staticmethod
    def is_dismissed(item: ActionItem, keys: frozenset[str] | set[str]) -> bool:
        return any(
            key in keys
            for key in (
                f"id|{item.item_id}",
                _dismissal_key(item),
                action_key(item),
                _legacy_key(item),
                completed_key(item),
                completed_key(item, legacy=True),
            )
        )

    async def dismissed(self) -> frozenset[str]:
        raw = await self.storage.get_preference("actions.dismissed")
        try:
            values = json.loads(raw or "[]")
            return (
                frozenset(str(value) for value in cast("list[Any]", values))
                if isinstance(values, list)
                else frozenset()
            )
        except (TypeError, ValueError):
            return frozenset()

    async def set_dismissed(self, keys: frozenset[str] | set[str]) -> None:
        await self.storage.set_preference(
            "actions.dismissed", json.dumps(sorted(keys), ensure_ascii=False)
        )

    async def read(
        self, *, deduplicate: bool = True, records: list[MailRecord] | None = None
    ) -> list[ScheduledAction]:
        """Share visible task state; overdue tasks remain until explicitly removed."""
        await self.recurring.materialize()
        dismissed = await self.dismissed()
        if records is None:
            records = await self.storage.list_mails()
        custom = await self.storage.list_custom_actions()
        overridden = {item.item_id for item in custom}
        entries = [
            ScheduledAction(item, record)
            for record in records
            for item in record.action_items
            if item.item_id not in overridden
            and not self.is_dismissed(item, dismissed)
            and item.completed_at is None
        ]
        entries.extend(
            ScheduledAction(item, custom=True) for item in custom if item.completed_at is None
        )
        entries.sort(key=lambda entry: action_sort_time(entry.item))
        if not deduplicate:
            return entries
        kept: list[ScheduledAction] = []
        by_summary: dict[tuple[str, str], list[ScheduledAction]] = {}
        for entry in entries:
            key = (entry.item.mail_id, " ".join(entry.item.summary.split()).casefold())
            siblings = by_summary.setdefault(key, [])
            if not any(actions_duplicate(entry.item, prior.item) for prior in siblings):
                siblings.append(entry)
                kept.append(entry)
        return kept

    @staticmethod
    def mail_view(records: list[MailRecord], entries: list[ScheduledAction]) -> list[MailRecord]:
        """Project the schedule into mail views without modifying stored analyses."""
        by_mail: dict[str, list[ActionItem]] = {}
        for entry in entries:
            mail_id = entry.record.record_id if entry.record is not None else entry.item.mail_id
            if mail_id:
                by_mail.setdefault(mail_id, []).append(entry.item)
        return [
            record.model_copy(
                update={
                    "analysis": record.analysis.model_copy(
                        update={"action_items": by_mail.get(record.record_id, [])}
                    )
                }
            )
            if record.analysis is not None
            else record
            for record in records
        ]

    async def remove(self, target: ScheduledAction, entries: list[ScheduledAction]) -> bool:
        """Caller holds lock, so dismissals cannot overwrite another deletion."""
        if target.custom:
            return await self.storage.delete_custom_action(target.item.item_id)
        matches = [
            entry
            for entry in entries
            if not entry.custom
            and (
                entry.item.item_id == target.item.item_id
                or actions_duplicate(entry.item, target.item)
            )
        ]
        keys = {
            key
            for entry in matches
            for key in (f"id|{entry.item.item_id}", _dismissal_key(entry.item))
        }
        await self.set_dismissed(await self.dismissed() | keys)
        return bool(keys)

    async def complete(self, target: ScheduledAction, entries: list[ScheduledAction]) -> None:
        """Remove the occurrence, retaining only opaque suppression keys for analyzed mail."""
        matches = [
            entry
            for entry in entries
            if entry.item.item_id == target.item.item_id
            or (not target.custom and actions_duplicate(entry.item, target.item))
        ]
        suppressed = {completed_key(entry.item) for entry in matches if entry.item.mail_id}
        if suppressed:
            await self.set_dismissed(await self.dismissed() | suppressed)
        records: dict[str, MailRecord] = {}
        completed_ids = {entry.item.item_id for entry in matches}
        for entry in matches:
            if entry.custom:
                await self.storage.delete_custom_action(entry.item.item_id)
            if entry.record is not None and entry.record.analysis is not None:
                records[entry.record.record_id] = entry.record.model_copy(deep=True)
        for record in records.values():
            assert record.analysis is not None
            record.analysis.action_items = [
                item for item in record.action_items if item.item_id not in completed_ids
            ]
            await self.storage.save_mail(record)

    async def trash_mail(
        self,
        record: MailRecord,
        *,
        delete_actions: bool = False,
        entries: list[ScheduledAction] | None = None,
    ) -> None:
        """Caller holds schedule and mail-write locks; keep task history and source context."""
        if entries is None:
            entries = await self.read(deduplicate=False, records=[record])
        for entry in entries:
            linked = entry.record.record_id if entry.record else entry.item.mail_id
            if linked != record.record_id:
                continue
            if delete_actions:
                await self.remove(entry, entries)
            elif not entry.custom:
                await self.storage.save_custom_action(
                    entry.item.model_copy(
                        update={
                            "mail_id": record.record_id,
                            "origin": ActionOrigin.CUSTOM,
                            "source_subject": record.mail.subject,
                        }
                    )
                )
        await self.storage.delete_mail(record.record_id, refresh_deleted_at=True)
        for identity in {record.record_id, record.mail.normalized_message_id()}:
            await self.storage.set_preference(f"mail.deleted.{identity}", "true")
