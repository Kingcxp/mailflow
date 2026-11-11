"""Visible schedules, durable delivery receipts and full event windows."""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from mailflow.config import MailFlowConfig, NotifierConfig
from mailflow.domain import ActionItem, ActionOrigin, MailAnalysis, Urgency
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine
from mailflow.registry import ComponentRegistry
from mailflow.service import MailFlowService

from mailflow_test_support.service import MemoryStorage, make_record


class FakeNotifier:
    backend_id = "fake"

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.sent: list[str] = []

    async def push_text(self, text: str) -> None:
        await asyncio.sleep(0)
        if self.failures:
            self.failures -= 1
            raise OSError("fake delivery failure")
        self.sent.append(text)


def make_service(storage: MemoryStorage, *notifiers: FakeNotifier) -> MailFlowService:
    return MailFlowService(
        config=MailFlowConfig.model_validate({"general": {"reminder_hour": 0}}),
        registry=ComponentRegistry(),
        plugin_manager=cast(Any, None),
        storage=cast(Any, storage),
        sources={},
        router=cast(Any, None),
        pipeline=PipelineEngine([]),
        notifiers=cast(Any, list(notifiers)),
        notifier_configs=[
            NotifierConfig(notifier_id=f"n{i}", provider="fake") for i in range(len(notifiers))
        ],
        events=EventBus(),
        i18n=I18n(),
    )


async def schedule(storage: MemoryStorage) -> ActionItem:
    now = datetime.now(UTC)
    item = ActionItem(
        item_id="action-1",
        mail_id="m1",
        summary="Meeting",
        action_type="meeting",
        due_at=now + timedelta(days=2),
    )
    record = make_record()
    record.analysis = MailAnalysis(summary="s", urgency=Urgency.INFO, action_items=[item])
    await storage.save_mail(record)
    await storage.set_preference(f"digest.{now.date().isoformat()}", "fired")
    return item


async def test_deleted_mail_action_disappears_from_search_reminders_and_digest() -> None:
    storage = MemoryStorage()
    item = await schedule(storage)
    notifier = FakeNotifier()
    service = make_service(storage, notifier)
    assert await service.delete_action(item.item_id)
    assert await service.list_actions() == await service.list_actions_all() == []
    assert await service.runtime.run_reminder_tick() == 0
    assert await service.runtime._approaching_actions(datetime.now(UTC), 3) == []  # pyright: ignore[reportPrivateUsage]
    assert notifier.sent == []


async def test_deleting_one_task_keeps_distinct_task_at_same_time() -> None:
    storage = MemoryStorage()
    item = await schedule(storage)
    other = item.model_copy(update={"item_id": "other", "summary": "Submit report"})
    record = storage.mails["m1"]
    assert record.analysis is not None
    record.analysis.action_items.append(other)
    service = make_service(storage)
    assert await service.delete_action(item.item_id)
    assert [a.item_id for a in await service.list_actions()] == ["other"]


async def test_partial_failure_retries_only_failed_destination_after_restart() -> None:
    storage = MemoryStorage()
    await schedule(storage)
    first, second = FakeNotifier(), FakeNotifier(failures=1)
    service = make_service(storage, first, second)
    assert await service.runtime.run_reminder_tick() == 0
    assert len(first.sent) == 1 and second.sent == []
    fresh = make_service(storage, first, second)
    assert await fresh.runtime.run_reminder_tick() == 1
    assert await fresh.runtime.run_reminder_tick() == 0
    assert len(first.sent) == len(second.sent) == 1


async def test_concurrent_scans_deliver_one_reminder() -> None:
    storage = MemoryStorage()
    await schedule(storage)
    notifier = FakeNotifier()
    service = make_service(storage, notifier)
    fired = await asyncio.gather(*(service.runtime.run_reminder_tick() for _ in range(6)))
    assert sum(fired) == len(notifier.sent) == 1


async def test_replacing_duplicate_action_id_does_not_send_same_reminder_again() -> None:
    storage = MemoryStorage()
    item = await schedule(storage)
    notifier = FakeNotifier()
    service = make_service(storage, notifier)
    assert await service.runtime.run_reminder_tick() == 1
    record = storage.mails["m1"]
    assert record.analysis is not None
    record.analysis.action_items = [item.model_copy(update={"item_id": "replacement"})]
    fresh = make_service(storage, notifier)
    assert await fresh.runtime.run_reminder_tick() == 0
    assert len(notifier.sent) == 1


async def test_partial_target_failure_does_not_resend_successful_chat() -> None:
    class TargetNotifier(FakeNotifier):
        async def push_to_target(self, target: str, text: str) -> None:
            if target == "group:2" and self.failures:
                self.failures -= 1
                raise OSError("chat unavailable")
            self.sent.append(target)

    storage = MemoryStorage()
    await schedule(storage)
    notifier = TargetNotifier(failures=1)
    service = make_service(storage, notifier)
    service.runtime._notifier_configs[0].options["targets"] = ["group:1", "group:2"]  # pyright: ignore[reportPrivateUsage]
    assert await service.runtime.run_reminder_tick() == 0
    assert await service.runtime.run_reminder_tick() == 1
    assert notifier.sent == ["group:1", "group:2"]


@pytest.mark.parametrize(
    "provider,klass,setting",
    [
        ("onebot", "OneBotNotifier", "http_url"),
        ("openwechat", "OpenWechatNotifier", "gateway_url"),
        ("whatsapp", "WhatsappNotifier", "gateway_url"),
        ("wechatpadpro", "WechatPadProNotifier", "base_url"),
    ],
)
@pytest.mark.parametrize("status", [503, 200])
async def test_rejected_gateway_push_raises_for_scheduler_retry(
    provider: str, klass: str, setting: str, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module(f"mailflow_notify_{provider}.plugin")
    notifier_class = getattr(module, klass)
    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, json={"retcode": 1, "Code": 1, "ok": False})
    )

    def fake_client(**kwargs: Any) -> httpx.AsyncClient:
        return client_class(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    notifier = notifier_class(
        NotifierConfig(
            notifier_id="n",
            provider=provider,
            options={setting: "http://fake", "auth_key": "fake", "targets": ["group:1"]},
        )
    )
    from mailflow.notifications import DeliveryError

    with pytest.raises(httpx.HTTPStatusError if status == 503 else DeliveryError):
        await notifier.push_to_target("group:1", "Reminder")
    with pytest.raises(httpx.HTTPStatusError if status == 503 else DeliveryError):
        await notifier.notify(make_record())


async def test_mail_target_failure_is_visible_and_retry_does_not_repeat_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mailflow_test_support.runtime import build_runtime, make_mail

    class Channel:
        minimum = Urgency.INFO

        def __init__(self) -> None:
            self.sent: list[str] = []
            self.fail = True

        async def notify_to_target(
            self, target: str, record: Any, *, text: str | None = None
        ) -> None:
            if target == "group:2" and self.fail:
                raise ConnectionError("offline")
            self.sent.append(target)

    original_sleep = asyncio.sleep

    async def no_delay(*args: Any) -> None:
        await original_sleep(0)

    monkeypatch.setattr("mailflow.runtime.asyncio.sleep", no_delay)
    channel = Channel()
    runtime, _, _ = build_runtime({}, notifiers=cast(Any, [channel]))
    runtime._notifier_configs[0].options["targets"] = ["group:1", "group:2"]  # pyright: ignore[reportPrivateUsage]
    await runtime.process_mail_now(make_mail())
    failures = await runtime.failed_deliveries()
    assert channel.sent == ["group:1"]
    assert len(failures) == 1 and failures[0]["target"] == "group:2"
    assert failures[0]["error_code"] == "errors.connection"
    channel.fail = False
    assert await runtime.retry_deliveries([failures[0]["delivery_id"]]) == 1
    assert channel.sent == ["group:1", "group:2"]


async def test_custom_storage_owns_deletion_even_with_legacy_analysis_origin() -> None:
    storage = MemoryStorage()
    item = await schedule(storage)
    storage.mails.clear()
    await storage.save_custom_action(item)
    service = make_service(storage)
    assert await service.delete_action("action-")
    assert storage.custom_actions == {}
    assert await service.runtime.action_store.dismissed() == frozenset()


@pytest.mark.parametrize("origin", [ActionOrigin.CUSTOM, ActionOrigin.ANALYSIS])
async def test_expiry_and_retention_preserve_ongoing_window(origin: ActionOrigin) -> None:
    storage = MemoryStorage()
    item = await schedule(storage)
    now = datetime.now(UTC)
    item.due_at, item.due_end, item.origin = (
        now - timedelta(days=3),
        now + timedelta(hours=1),
        origin,
    )
    if origin is not ActionOrigin.ANALYSIS:
        storage.mails["m1"].analysis = MailAnalysis(summary="s", urgency=Urgency.INFO)
        await storage.save_custom_action(item)
    service = make_service(storage)
    assert await service.purge_expired_actions() == 0
    assert await service.list_expired_mails() == []
    await service.runtime.run_cleanup()
    assert "m1" in storage.mails
    assert [a.item_id for a in await service.list_actions()] == [item.item_id]


async def test_daily_digest_uses_local_dates_and_retries_failed_delivery() -> None:
    storage = MemoryStorage()
    now = datetime(2099, 3, 8, 6, 30, tzinfo=UTC)  # New York: 01:30, DST starts today.
    for item_id, start, end in [
        ("ongoing", now - timedelta(days=2), now + timedelta(hours=1)),
        ("today", datetime(2099, 3, 9, 3, 30, tzinfo=UTC), None),
        ("tomorrow", datetime(2099, 3, 9, 5, 30, tzinfo=UTC), None),
    ]:
        await storage.save_custom_action(
            ActionItem(
                item_id=item_id,
                mail_id="",
                summary=item_id,
                action_type="meeting",
                due_at=start,
                due_end=end,
            )
        )
    notifier = FakeNotifier(failures=1)
    service = make_service(storage, notifier)
    service.config.general.timezone = "America/New_York"
    service.config.general.reminder_days_before = 0
    runtime = service.runtime
    assert await runtime._fire_daily_digest(now, service.config.general) == 0  # pyright: ignore[reportPrivateUsage]
    assert await storage.get_preference("digest.2099-03-08") is None
    assert await runtime._fire_daily_digest(now, service.config.general) == 1  # pyright: ignore[reportPrivateUsage]
    assert await runtime._fire_daily_digest(now, service.config.general) == 0  # pyright: ignore[reportPrivateUsage]
    assert len(notifier.sent) == 1
    assert "ongoing" in notifier.sent[0] and "today" in notifier.sent[0]
    assert "tomorrow" not in notifier.sent[0]


@pytest.mark.parametrize("state", ["overdue", "snoozed", "completed", "disabled"])
async def test_ineligible_tasks_stay_visible_without_reminders_or_digest(state: str) -> None:
    storage = MemoryStorage()
    item = await schedule(storage)
    now = datetime.now(UTC)
    item.due_at = now.replace(hour=23, minute=59, second=59)
    if state == "overdue":
        item.due_at = now - timedelta(days=30)
    elif state == "snoozed":
        item.snoozed_until = now + timedelta(hours=1)
    elif state == "completed":
        item.completed_at = now
    else:
        item.reminder_enabled = False
    await storage.set_preference(f"digest.{now.date().isoformat()}", "")
    notifier = FakeNotifier()
    service = make_service(storage, notifier)
    assert await service.runtime.run_reminder_tick() == 0
    assert notifier.sent == []
    assert len(await service.list_actions_all()) == (0 if state == "completed" else 1)


async def test_quiet_import_and_reanalysis_keep_reminder_history_and_local_state() -> None:
    from mailflow_test_support.runtime import build_runtime, make_mail

    class Recorder:
        minimum = Urgency.INFO

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def notify(self, record: Any) -> None:
            self.sent.append("mail")

        async def push_text(self, text: str) -> None:
            self.sent.append(text)

    now = datetime.now(UTC)
    action = ActionItem(
        item_id="model",
        mail_id="",
        summary="Submit report",
        action_type="task",
        due_at=now.replace(hour=23, minute=59, second=59),
    )
    analysis = MailAnalysis(summary="mail", urgency=Urgency.INFO, action_items=[action])
    notifier = Recorder()
    runtime, _, _ = build_runtime(
        {},
        analysis=analysis,
        notifiers=cast(Any, [notifier]),
        reminder_hour=0,
        daily_digest=False,
    )
    mail = make_mail()
    original = await runtime.process_mail_now(mail)
    assert original is not None
    assert await runtime.run_reminder_tick() == 1
    assert len(notifier.sent) == 2
    original.action_items[0].reminder_enabled = False
    original.action_items[0].completed_at = now
    analysis.action_items[0].summary = "Submit report."
    repeated = await runtime.process_mail_now(mail, force=True)
    assert repeated is not None
    assert repeated.action_items[0].item_id == original.action_items[0].item_id
    assert repeated.action_items[0].completed_at == now
    assert not repeated.action_items[0].reminder_enabled
    assert await runtime.run_reminder_tick() == 0
    assert len(notifier.sent) == 2
    history = make_mail().model_copy(update={"ingestion": "history"})
    analysis.action_items[0].summary = "Another task"
    await runtime.process_mail_now(history)
    assert await runtime.run_reminder_tick() == 0
    assert len(notifier.sent) == 2


async def test_digest_covers_same_day_reminder_but_observing_events_is_not_delivery() -> None:
    storage = MemoryStorage()
    item = await schedule(storage)
    now = datetime.now(UTC)
    await storage.set_preference(f"digest.{now.date().isoformat()}", "")
    notifier = FakeNotifier()
    service = make_service(storage, notifier)
    service.config.general.timezone = "UTC"
    assert await service.runtime.run_reminder_tick() == 1
    assert len(notifier.sent) == 1  # The digest already included this task.
    assert item.summary in notifier.sent[0]
    assert await service.runtime.run_reminder_tick() == 0
    assert len(notifier.sent) == 1

    another = MemoryStorage()
    await schedule(another)
    observer = make_service(another)

    async def log_only(**payload: Any) -> None:
        pass

    observer.on("mailflow.action.reminder", log_only)
    assert await observer.runtime.run_reminder_tick() == 0
    assert not any(
        value == "fired"
        for key, value in another.preferences.items()
        if key.startswith("reminder.")
    )


async def test_unsubscribed_recipient_cannot_be_retried_from_failures() -> None:
    storage = MemoryStorage()
    await schedule(storage)

    class Targeted(FakeNotifier):
        async def push_to_target(self, target: str, text: str) -> None:
            await self.push_text(text)

    notifier = Targeted(failures=1)
    service = make_service(storage, notifier)
    channel = service.runtime._notifier_configs[0]  # pyright: ignore[reportPrivateUsage]
    channel.options = {"targets": ["group:42"]}
    assert await service.runtime.run_reminder_tick() == 0
    failures = await service.failed_deliveries()
    assert len(failures) == 1
    channel.options["excluded_targets"] = ["group:42"]
    assert await service.retry_deliveries([failures[0]["delivery_id"]]) == 0
    assert notifier.sent == []
    assert await service.failed_deliveries() == []


async def test_retry_rechecks_task_completed_while_another_delivery_is_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class Channel(FakeNotifier):
        retrying = False

        async def notify(self, record: Any) -> None:
            if not self.retrying:
                raise ConnectionError("offline")
            started.set()
            await release.wait()

    original_sleep = asyncio.sleep

    async def no_delay(*args: Any) -> None:
        await original_sleep(0)

    monkeypatch.setattr("mailflow.runtime.asyncio.sleep", no_delay)
    storage = MemoryStorage()
    item = await schedule(storage)
    channel = Channel(failures=1)
    service = make_service(storage, channel)
    await service.runtime._notify(storage.mails["m1"])  # pyright: ignore[reportPrivateUsage]
    assert await service.runtime.run_reminder_tick() == 0
    failures = await service.failed_deliveries()
    assert len(failures) == 2
    channel.retrying = True
    retry = asyncio.create_task(service.retry_deliveries([row["delivery_id"] for row in failures]))
    try:
        await asyncio.wait_for(started.wait(), 1)
        await service.edit_action(item.item_id, completed=True)
        release.set()
        assert await retry == 1  # Only the mail was delivered.
        assert channel.sent == []
        assert await service.failed_deliveries() == []
    finally:
        release.set()
        await retry
