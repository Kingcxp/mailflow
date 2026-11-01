"""The shared view of visible schedule entries and their storage ownership."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from mailflow.contracts import StorageBackend
from mailflow.domain import ActionItem, MailRecord, action_time_window, actions_duplicate, to_utc


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
    return f"{item.mail_id}|{to_utc(item.due_at).isoformat()}|{item.action_type}"


def action_has_ended(item: ActionItem, now: datetime) -> bool:
    return action_time_window(item)[1] <= to_utc(now)


@dataclass
class ScheduledAction:
    item: ActionItem
    record: MailRecord | None = None
    custom: bool = False


class ActionStore:
    def __init__(self, storage: StorageBackend) -> None:
        self.storage = storage
        self.lock = asyncio.Lock()

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

    async def read(self, *, deduplicate: bool = True) -> list[ScheduledAction]:
        """Deleted mail actions stay hidden; custom/seminar rows own their deletion.

        Recently ended entries remain visible until the expiry sweep retires
        them. Reminder and cleanup callers use the same end-of-window rule.
        """
        dismissed = await self.dismissed()
        entries = [
            ScheduledAction(item, record)
            for record in await self.storage.list_mails()
            for item in record.action_items
            if action_key(item) not in dismissed and _legacy_key(item) not in dismissed
        ]
        entries.extend(
            ScheduledAction(item, custom=True) for item in await self.storage.list_custom_actions()
        )
        entries.sort(key=lambda entry: to_utc(entry.item.due_at))
        if not deduplicate:
            return entries
        kept: list[ScheduledAction] = []
        for entry in entries:
            if not any(actions_duplicate(entry.item, prior.item) for prior in kept):
                kept.append(entry)
        return kept

    async def remove(self, target: ScheduledAction, entries: list[ScheduledAction]) -> bool:
        """Caller holds lock, so dismissals cannot overwrite another deletion."""
        if target.custom:
            return await self.storage.delete_custom_action(target.item.item_id)
        keys = {
            action_key(entry.item)
            for entry in entries
            if not entry.custom
            and (
                action_key(entry.item) == action_key(target.item)
                or actions_duplicate(entry.item, target.item)
            )
        }
        await self.set_dismissed(await self.dismissed() | keys)
        return bool(keys)
