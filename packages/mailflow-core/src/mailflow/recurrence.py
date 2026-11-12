"""Recurring plans materialize only the current occurrence, using the normal action store."""

from __future__ import annotations

import asyncio
import calendar
import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from mailflow.contracts import StorageBackend
from mailflow.domain import ActionItem, ActionPlan, to_utc


def resolve_schedule(plan: ActionPlan) -> None:
    """Keep an omitted day anchored to the plan, including in month-end previews."""
    if plan.template.due_at is None:
        return
    local = plan.template.due_at.astimezone(ZoneInfo(plan.timezone))
    if plan.schedule.frequency == "monthly" and plan.schedule.month_day is None:
        plan.schedule.month_day = local.day
    elif plan.schedule.frequency == "weekly" and not plan.schedule.weekdays:
        plan.schedule.weekdays = [local.weekday()]


def occurrence_on(plan: ActionPlan, day: date) -> datetime | None:
    start = plan.template.due_at
    assert start is not None
    local = start.astimezone(ZoneInfo(plan.timezone))
    if day < local.date():
        return None
    schedule = plan.schedule
    if schedule.frequency == "weekly" and day.weekday() not in (
        schedule.weekdays or [local.weekday()]
    ):
        return None
    if schedule.frequency == "monthly" and day.day != min(
        schedule.month_day or local.day, calendar.monthrange(day.year, day.month)[1]
    ):
        return None
    return datetime.combine(day, local.timetz()).astimezone(UTC)


def occurrence(plan: ActionPlan, when: datetime, *, preview: bool = False) -> ActionItem:
    template = plan.template
    duration = (
        template.due_end - template.due_at
        if template.due_end is not None and template.due_at is not None
        else None
    )
    return template.model_copy(
        update={
            "item_id": f"{plan.plan_id}.{when.strftime('%Y%m%dT%H%M%SZ')}",
            "plan_id": plan.plan_id,
            "repeat": plan.schedule,
            "due_at": when,
            "due_end": when + duration if duration is not None else None,
            "completed_at": None,
            "reminder_key": "",
            "preview": preview,
        },
        deep=True,
    )


class RecurringActions:
    def __init__(self, storage: StorageBackend) -> None:
        self.storage = storage
        self.lock = asyncio.Lock()

    async def read(self) -> list[ActionPlan]:
        raw = await self.storage.get_preference("actions.plans")
        plans = [ActionPlan.model_validate(row) for row in json.loads(raw or "[]")]
        for plan in plans:
            resolve_schedule(plan)
        return plans

    async def save(self, plans: list[ActionPlan]) -> None:
        await self.storage.set_preference(
            "actions.plans", json.dumps([p.model_dump(mode="json") for p in plans])
        )

    async def add(self, plan: ActionPlan) -> None:
        async with self.lock:
            resolve_schedule(plan)
            await self.save([*await self.read(), plan])

    async def stop(self, plan_id: str) -> bool:
        async with self.lock:
            plans = await self.read()
            kept = [plan for plan in plans if plan.plan_id != plan_id]
            if len(plans) == len(kept):
                return False
            await self.save(kept)
            return True

    async def materialize(self, now: datetime | None = None) -> None:
        now = to_utc(now or datetime.now(UTC))
        async with self.lock:
            plans = await self.read()
            changed = False
            existing = {item.item_id for item in await self.storage.list_custom_actions()}
            for plan in plans:
                today = now.astimezone(ZoneInfo(plan.timezone)).date()
                current = None
                # The previous monthly occurrence is at most 31 days away.
                for offset in range(32):
                    candidate = occurrence_on(plan, today - timedelta(days=offset))
                    if candidate is not None and candidate <= now:
                        current = candidate
                        break
                if current is None and plan.last_occurrence is None:
                    current = occurrence_on(plan, today)
                if current is None or (
                    plan.last_occurrence is not None and current <= plan.last_occurrence
                ):
                    continue
                item = occurrence(plan, current)
                if item.item_id not in existing:
                    await self.storage.save_custom_action(item)
                plan.last_occurrence = current
                changed = True
            if changed:
                await self.save(plans)

    async def preview(self, days: int = 30, now: datetime | None = None) -> list[ActionItem]:
        now = to_utc(now or datetime.now(UTC))
        existing = {item.item_id for item in await self.storage.list_custom_actions()}
        items: list[ActionItem] = []
        for plan in await self.read():
            today = now.astimezone(ZoneInfo(plan.timezone)).date()
            for offset in range(max(0, days) + 1):
                when = occurrence_on(plan, today + timedelta(days=offset))
                if when is None or when <= now or when > now + timedelta(days=days):
                    continue
                item = occurrence(plan, when, preview=True)
                if item.item_id not in existing and (
                    plan.last_occurrence is None or when > plan.last_occurrence
                ):
                    items.append(item)
        return sorted(items, key=lambda item: item.due_at or now)
