"""Existing notification switches, quiet windows and truthful delivery deferral."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, time
from functools import partial
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from mailflow.domain import MailRecord, Urgency
from mailflow.errors import error_code
from mailflow.notifications import format_mail, option_enabled, targets_of
from mailflow.permissions import require_admin

if TYPE_CHECKING:
    from mailflow.config import GeneralConfig
    from mailflow.runtime import MailFlowRuntime
    from mailflow.service import MailFlowService


def in_quiet_hours(config: GeneralConfig, now: datetime | None = None) -> bool:
    if not config.quiet_enabled:
        return False
    clock = (now or datetime.now(UTC)).astimezone(ZoneInfo(config.timezone)).time()
    start, end = time.fromisoformat(config.quiet_start), time.fromisoformat(config.quiet_end)
    return start <= clock < end if start < end else clock >= start or clock < end


async def preferences(service: MailFlowService, values: dict[str, Any]) -> dict[str, Any]:
    if values:
        require_admin(service.t)
        updated = service.config.model_copy(deep=True)
        preset = values.get("preset")
        if preset in {"instant", "important", "tasks"}:
            updated.general.mail_notifications = preset != "tasks"
            updated.general.task_reminders = True
            updated.general.daily_digest = False
            updated.general.hourly_summary = False
            for channel in updated.notifiers:
                channel.minimum_urgency = Urgency.IMPORTANT if preset == "important" else Urgency.AD
                channel.options.update(
                    mail_notifications=preset != "tasks",
                    task_reminders=True,
                    daily_digest=False,
                    hourly_summary=False,
                )
        for name in ("quiet_enabled", "quiet_start", "quiet_end"):
            if name in values:
                setattr(updated.general, name, values[name])
        # Revalidate time input without changing credentials, subscriptions or gateways.
        updated = type(updated).model_validate(updated.model_dump())
        previous = service.config
        await service._persist_config(updated, "general.notifications")  # pyright: ignore[reportPrivateUsage]
        if previous.notifiers != updated.notifiers:
            await service._rebuild_notifiers()  # pyright: ignore[reportPrivateUsage]
        if preset in {"instant", "important", "tasks", "custom"}:
            await service.storage.set_preference("notifications.preset", str(preset))
    general = service.config.general
    switches = (
        general.mail_notifications,
        general.task_reminders,
        general.daily_digest,
        general.hourly_summary,
    )
    preset = await service.storage.get_preference("notifications.preset") or "custom"
    if preset != "custom" and (
        switches != (preset != "tasks", True, False, False)
        or any(
            n.minimum_urgency != (Urgency.IMPORTANT if preset == "important" else Urgency.AD)
            or option_enabled(n.options, "mail_notifications") != (preset != "tasks")
            or not option_enabled(n.options, "task_reminders")
            or option_enabled(n.options, "daily_digest")
            or option_enabled(n.options, "hourly_summary")
            for n in service.config.notifiers
        )
    ):
        preset = "custom"
    return {
        "preset": preset,
        "quiet_enabled": general.quiet_enabled,
        "quiet_start": general.quiet_start,
        "quiet_end": general.quiet_end,
        "quiet_now": in_quiet_hours(general),
        "channels": [
            {
                "id": n.notifier_id,
                "enabled": n.enabled,
                "mail": n.enabled
                and general.mail_notifications
                and option_enabled(n.options, "mail_notifications"),
                "tasks": n.enabled
                and general.task_reminders
                and option_enabled(n.options, "task_reminders"),
                "reason": service.t("experience.channel_disabled")
                if not n.enabled
                else service.t("experience.global_disabled")
                if not general.mail_notifications
                else service.t("experience.channel_disabled")
                if not option_enabled(n.options, "mail_notifications")
                else "",
                "task_reason": service.t("experience.channel_disabled")
                if not n.enabled
                else service.t("experience.global_disabled")
                if not general.task_reminders
                else service.t("experience.channel_disabled")
                if not option_enabled(n.options, "task_reminders")
                else "",
            }
            for n in service.config.notifiers
        ],
    }


async def defer_mail(runtime: MailFlowRuntime, record: MailRecord) -> None:
    storage = runtime.storage
    rows = json.loads(await storage.get_preference("notifications.deferred") or "[]")
    if record.record_id not in rows:
        rows.append(record.record_id)
        await storage.set_preference("notifications.deferred", json.dumps(rows))


async def flush_mail(runtime: MailFlowRuntime) -> None:
    """Per-mail destination receipts let partial batches retry without resending successes."""
    storage = runtime.storage
    rows: list[str] = json.loads(await storage.get_preference("notifications.deferred") or "[]")
    if not rows:
        return
    retained: set[str] = set()
    failed = await runtime.deliveries.read()
    records = [
        r
        for ident in rows
        if (r := await storage.get_mail(ident)) is not None and not r.is_read and not r.archived
    ]
    for notifier, config in runtime.notification_channels():
        if (
            not config.enabled
            or not runtime.config.general.mail_notifications
            or not option_enabled(config.options, "mail_notifications")
        ):
            continue
        eligible = [r for r in records if r.effective_urgency.rank >= config.minimum_urgency.rank]
        if not callable(getattr(notifier, "push_text", None)):
            for record in eligible:
                await runtime._notify_unlocked(record, only=config.notifier_id)  # pyright: ignore[reportPrivateUsage]
                if not await storage.get_preference(
                    f"mail.delivery.{record.record_id}.{config.notifier_id}"
                ):
                    retained.add(record.record_id)
            continue
        targets = targets_of(config)
        if not targets and config.options.get("excluded_targets"):
            continue
        for target in (
            targets if targets and callable(getattr(notifier, "push_to_target", None)) else [None]
        ):
            identity = hashlib.sha256(f"{config.notifier_id}|{target or ''}".encode()).hexdigest()[
                :24
            ]
            keys = {
                r.record_id: f"mail.delivery.{r.record_id}.{config.notifier_id}"
                + (f".delivered.{identity}" if target is not None else "")
                for r in eligible
            }
            pending = [r for r in eligible if not await storage.get_preference(keys[r.record_id])]
            if not pending:
                continue
            previous = next(
                (
                    r
                    for r in failed.values()
                    if r.get("kind") == "deferred"
                    and r.get("channel") == config.notifier_id
                    and r.get("target") == (target or "")
                ),
                None,
            )
            if previous:
                frozen = set(previous["record_ids"])
                retained.update(r.record_id for r in pending if r.record_id not in frozen)
                pending = [r for r in pending if r.record_id in frozen]
                if not pending:
                    await runtime.deliveries.update(previous["delivery_id"], None)
                    continue
            receipt = (
                "deferred."
                + identity
                + "."
                + hashlib.sha256("|".join(r.record_id for r in pending).encode()).hexdigest()[:16]
            )
            text = (
                runtime.translate("experience.deferred_mail", count=len(pending))
                + "\n\n"
                + "\n\n".join(format_mail(r, runtime.translate) for r in pending)
            )
            try:
                await runtime._send_content(  # pyright: ignore[reportPrivateUsage]
                    notifier,
                    config,
                    target,
                    receipt,
                    text,
                    partial(runtime._send_text, notifier, target),  # pyright: ignore[reportPrivateUsage]
                    records=pending,
                    text_receipts=[keys[r.record_id] for r in pending],
                )
            except Exception as exc:
                retained.update(r.record_id for r in pending)
                await runtime.deliveries.update(
                    receipt,
                    {
                        "kind": "deferred",
                        "channel": config.notifier_id,
                        "target": target or "",
                        "error_code": error_code(exc),
                        "record_ids": [r.record_id for r in pending],
                    },
                )
            else:
                for record in pending:
                    await storage.set_preference(keys[record.record_id], "delivered")
                await storage.set_preference(receipt, "delivered")
                await runtime.deliveries.update(receipt, None)
    await storage.set_preference(
        "notifications.deferred", json.dumps([r for r in rows if r in retained])
    )
