"""Unit tests for the async runtime supervisor and retention scheduler."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mailflow.config import MailAccountConfig, MailFlowConfig, NotifierConfig
from mailflow.contracts import (
    MailEmitter,
    MailMessage,
    ProcessingContext,
    ProcessorResult,
)
from mailflow.domain import (
    ActionItem,
    MailAnalysis,
    MailRecord,
    ProcessorNote,
    Urgency,
)
from mailflow.events import EventBus
from mailflow.pipeline import PipelineEngine, ProcessorBinding
from mailflow.runtime import MailFlowRuntime, reminder_times, seconds_until_next_cleanup

from mailflow_test_support.runtime import (
    EmittingSource,
    FakeStorage,
    RecordingNotifier,
    StaticProcessor,
    build_runtime,
    make_mail,
)


class TestCleanupScheduler:
    def test_before_cleanup_same_day(self) -> None:
        now = datetime(2026, 3, 1, 2, 0, tzinfo=UTC)
        assert seconds_until_next_cleanup(now, "UTC", 4) == 2 * 3600

    def test_after_cleanup_rolls_to_next_day(self) -> None:
        now = datetime(2026, 3, 1, 5, 30, tzinfo=UTC)
        assert seconds_until_next_cleanup(now, "UTC", 4) == pytest.approx(22.5 * 3600)

    def test_exactly_at_cleanup_rolls_to_next_day(self) -> None:
        now = datetime(2026, 3, 1, 4, 0, tzinfo=UTC)
        assert seconds_until_next_cleanup(now, "UTC", 4) == pytest.approx(24 * 3600)

    def test_minute_config(self) -> None:
        now = datetime(2026, 3, 1, 4, 0, tzinfo=UTC)
        assert seconds_until_next_cleanup(now, "UTC", 4, minute=15) == 15 * 60

    def test_timezone_aware(self) -> None:
        # 2026-03-01 00:30 UTC == 08:30 Asia/Shanghai -> next cleanup 04:00 +1d
        now = datetime(2026, 3, 1, 0, 30, tzinfo=UTC)
        assert seconds_until_next_cleanup(now, "Asia/Shanghai", 4) == pytest.approx(19.5 * 3600)


class TestRuntime:
    async def test_notifier_threshold_suppresses_low_urgency(self) -> None:
        analysis = MailAnalysis(summary="ad", urgency=Urgency.AD)
        source = EmittingSource([make_mail()])
        notifier = RecordingNotifier(minimum=Urgency.IMPORTANT)
        runtime, storage, _ = build_runtime(
            {"acct-1": source},
            analysis=analysis,
            workers=2,
            accounts=[MailAccountConfig(account_id="acct-1", provider="fake")],
            notifiers=[notifier],
        )
        await runtime.start()
        await asyncio.sleep(0.3)
        await runtime.stop()
        assert len(storage.saved) == 1
        assert notifier.notified == []

    async def test_one_source_failure_does_not_stop_others(self) -> None:
        broken = EmittingSource([], fail=True)
        healthy = EmittingSource([make_mail(account_id="acct-2")])
        runtime, storage, _ = build_runtime(
            {"acct-1": broken, "acct-2": healthy},
            workers=2,
            accounts=[
                MailAccountConfig(account_id="acct-1", provider="fake"),
                MailAccountConfig(account_id="acct-2", provider="fake2"),
            ],
        )
        await runtime.start()
        await asyncio.sleep(0.3)
        await runtime.stop()
        assert runtime.account_status("acct-1") == "error"
        assert runtime.account_error("acct-1") is not None
        assert len(storage.saved) == 1
        assert storage.saved[0].mail.account_id == "acct-2"

    async def test_run_cleanup_cutoffs(self) -> None:
        runtime, storage, _ = build_runtime({}, mail_retention_days=30, trash_retention_days=7)
        old = MailRecord(record_id="old", mail=make_mail(), received_at=make_mail().received_at)
        starred = old.model_copy(update={"record_id": "starred", "starred": True})
        storage.saved = [old, starred]
        assert await runtime.run_cleanup() == (1, 0)
        assert [r.record_id for r in storage.saved] == ["starred"]
        assert storage.purged_before is not None
        expected_trash = datetime.now(UTC) - timedelta(days=7)
        assert abs((storage.purged_before - expected_trash).total_seconds()) < 60

    async def test_disabled_account_not_started(self) -> None:
        runtime, storage, _ = build_runtime(
            {"off": EmittingSource([make_mail()])},
            accounts=[MailAccountConfig(account_id="off", provider="fake", enabled=False)],
        )
        await runtime.start()
        await asyncio.sleep(0.1)
        await runtime.stop()
        assert runtime.account_status("off") == "stopped"
        assert storage.saved == []

    async def test_transient_save_failure_is_retried(self) -> None:
        """A mail must not be lost to a transient storage error."""
        config = MailFlowConfig.model_validate({"general": {"workers": 1}})
        storage = FakeStorage()

        class FlakySaveStorage(FakeStorage):
            def __init__(self) -> None:
                super().__init__()
                self.failures_left = 2

            async def save_mail(self, record: MailRecord) -> None:
                if self.failures_left > 0:
                    self.failures_left -= 1
                    raise RuntimeError("disk full (transient)")
                await super().save_mail(record)

        storage = FlakySaveStorage()
        processor = StaticProcessor(MailAnalysis(summary="s", urgency=Urgency.INFO))
        engine = PipelineEngine(
            [ProcessorBinding(processor_id="p", plugin_id="t", processor=processor)]
        )
        source = EmittingSource([make_mail()])
        runtime = MailFlowRuntime(
            config,
            sources={"acct-1": source},
            pipeline=engine,
            storage=storage,
            notifiers=[],
            notifier_configs=[],
            events=EventBus(),
            account_configs=[MailAccountConfig(account_id="acct-1", provider="fake")],
        )
        await runtime.start()
        await asyncio.wait_for(runtime._queue.join(), timeout=4)  # pyright: ignore[reportPrivateUsage]
        for _ in range(50):
            if storage.saved and storage.saved[-1].analysis_state == "ready":
                break
            await asyncio.sleep(0.05)
        await runtime.stop()
        assert len(storage.saved) == 1  # recovered after transient failures


class TestReminderTimes:
    def test_early_and_midnight_windows(self) -> None:
        from mailflow.domain import ActionItem

        item = ActionItem(
            item_id="a1",
            mail_id="m1",
            summary="exam",
            action_type="exam",
            due_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
        )
        times = reminder_times(item, "UTC", 2, 8, 0)
        assert next(when for when, kind in times if kind == "early") == datetime(
            2026, 6, 8, 8, 0, tzinfo=UTC
        )
        assert next(when for when, kind in times if kind == "day_of") == datetime(
            2026, 6, 10, 0, 0, tzinfo=UTC
        )

    def test_timezone_applied(self) -> None:
        from mailflow.domain import ActionItem

        item = ActionItem(
            item_id="a1",
            mail_id="m1",
            summary="s",
            action_type="other",
            due_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
        )
        early = next(
            when for when, kind in reminder_times(item, "Asia/Shanghai", 2, 8, 0) if kind == "early"
        )
        assert early.tzinfo is not None
        assert early.utcoffset().total_seconds() == 8 * 3600  # type: ignore[union-attr]


class TestReminderScheduler:
    async def _runtime_with_item(
        self, item: Any, storage: FakeStorage | None = None
    ) -> tuple[MailFlowRuntime, EventBus]:
        from mailflow.domain import MailAnalysis

        storage = storage or FakeStorage()
        record = MailRecord(
            record_id="r1",
            mail=make_mail(),
            auto_urgency=Urgency.URGENT,
            analysis=MailAnalysis(summary="s", urgency=Urgency.URGENT, action_items=[item]),
        )
        await storage.save_mail(record)
        events = EventBus()
        config = MailFlowConfig.model_validate(
            {
                "general": {
                    "workers": 1,
                    "reminder_interval_seconds": 10,
                    "reminder_hour": 0,
                    "reminder_minute": 0,
                }
            }
        )
        runtime = MailFlowRuntime(
            config,
            sources={},
            pipeline=PipelineEngine([]),
            storage=storage,
            notifiers=[],
            notifier_configs=[],
            events=events,
            account_configs=[],
        )
        return runtime, events

    async def test_early_reminder_fires_once(self) -> None:
        from mailflow.domain import ActionItem

        # due in two days: the early window (due-2d 08:00) is already past
        item = ActionItem(
            item_id="a1",
            mail_id="r1",
            summary="Collect student ID",
            action_type="errand",
            due_at=datetime.now(UTC) + timedelta(days=2),
            notes="bring photo",
        )
        runtime, events = await self._runtime_with_item(item)
        reminders: list[dict[str, Any]] = []

        async def capture(event: str, **payload: Any) -> bool:
            reminders.append(payload)
            return True

        events.subscribe("mailflow.action.reminder", capture)
        fired = await runtime.run_reminder_tick()
        assert fired == 1
        assert reminders[0]["kind"] == "early"
        assert reminders[0]["item"].item_id == "a1"
        assert reminders[0]["record"].record_id == "r1"
        # second tick must not re-fire
        assert await runtime.run_reminder_tick() == 0
        assert len(reminders) == 1

    async def test_day_of_reminder_fires_for_today(self) -> None:
        from mailflow.domain import ActionItem

        # due later today: midnight window is already past
        item = ActionItem(
            item_id="a2",
            mail_id="r1",
            summary="Exam today",
            action_type="exam",
            due_at=datetime.now(UTC) + timedelta(hours=3),
        )
        runtime, events = await self._runtime_with_item(item)
        reminders: list[dict[str, Any]] = []

        async def capture(event: str, **payload: Any) -> bool:
            reminders.append(payload)
            return True

        events.subscribe("mailflow.action.reminder", capture)
        fired = await runtime.run_reminder_tick()
        assert fired == 1
        assert len(reminders) == 1
        assert await runtime.run_reminder_tick() == 0

    async def test_future_item_does_not_fire(self) -> None:
        from mailflow.domain import ActionItem

        item = ActionItem(
            item_id="a3",
            mail_id="r1",
            summary="Meeting next week",
            action_type="meeting",
            due_at=datetime.now(UTC) + timedelta(days=10),
        )
        runtime, _ = await self._runtime_with_item(item)
        assert await runtime.run_reminder_tick() == 0


class TestCustomActionReminders:
    async def test_user_created_action_fires_reminder(self) -> None:
        """Custom action items (no source mail) enter the reminder scheduler."""
        from mailflow.domain import ActionItem, MailAnalysis

        storage = FakeStorage()
        item = ActionItem(
            item_id="user-1",
            mail_id="",
            summary="Water the plants",
            action_type="errand",
            due_at=datetime.now(UTC) + timedelta(days=2),
            notes="",
        )
        await storage.save_custom_action(item)
        record = MailRecord(
            record_id="r1",
            mail=make_mail(),
            auto_urgency=Urgency.URGENT,
            analysis=MailAnalysis(summary="s", urgency=Urgency.URGENT, action_items=[]),
        )
        await storage.save_mail(record)
        events = EventBus()
        config = MailFlowConfig.model_validate(
            {
                "general": {
                    "workers": 1,
                    "reminder_interval_seconds": 10,
                    "reminder_hour": 0,
                    "reminder_minute": 0,
                }
            }
        )
        runtime = MailFlowRuntime(
            config,
            sources={},
            pipeline=PipelineEngine([]),
            storage=storage,
            notifiers=[],
            notifier_configs=[],
            events=events,
            account_configs=[],
        )
        reminders: list[dict[str, Any]] = []

        async def capture(event: str, **payload: Any) -> bool:
            reminders.append(payload)
            return True

        events.subscribe("mailflow.action.reminder", capture)
        fired = await runtime.run_reminder_tick()
        assert fired == 1
        assert reminders[0]["item"].item_id == "user-1"
        assert reminders[0]["record"] is None  # no source mail
        assert await runtime.run_reminder_tick() == 0
        assert len(reminders) == 1


class TestDeduplication:
    async def test_identical_message_ids_in_different_accounts_stay_separate(self) -> None:
        """Each account owns its own copy and can apply its own local state."""
        analysis = MailAnalysis(summary="dup", urgency=Urgency.IMPORTANT)
        m1 = make_mail(account_id="acct-1")
        m2 = m1.model_copy(update={"account_id": "acct-2"})  # forwarded copy
        runtime, storage, notifiers = build_runtime(
            {"acct-1": EmittingSource([m1]), "acct-2": EmittingSource([m2])},
            analysis=analysis,
            workers=2,
            accounts=[
                MailAccountConfig(account_id="acct-1", provider="fake"),
                MailAccountConfig(account_id="acct-2", provider="fake"),
            ],
            notifiers=[RecordingNotifier()],
        )
        await runtime.start()
        await asyncio.sleep(0.4)
        await runtime.stop()
        assert len(storage.saved) == 2
        assert len(notifiers[0].notified) == 2
        assert {record.mail.account_id for record in storage.saved} == {"acct-1", "acct-2"}

    async def test_similar_tasks_from_other_sources_survive_reanalysis(self) -> None:
        storage = FakeStorage()
        old_mail = make_mail(subject="Old notice")
        due = datetime(2099, 5, 1, 9, tzinfo=UTC)
        old_action = ActionItem(
            item_id="old-action",
            mail_id=old_mail.message_id,
            summary="Pay the fee",
            action_type="errand",
            due_at=due,
        )
        old_record = MailRecord(
            record_id=old_mail.normalized_message_id(),
            mail=old_mail,
            auto_urgency=Urgency.IMPORTANT,
            analysis=MailAnalysis(
                summary="old", urgency=Urgency.IMPORTANT, action_items=[old_action]
            ),
        )
        await storage.save_mail(old_record)
        custom_action = ActionItem(
            item_id="custom-action",
            mail_id="",
            summary="Submit report",
            action_type="errand",
            due_at=due,
        )
        await storage.save_custom_action(custom_action)
        new_mail = make_mail(subject="Another notice")
        new_analysis = MailAnalysis(
            summary="new",
            urgency=Urgency.IMPORTANT,
            action_items=[
                ActionItem(
                    item_id="new-old-duplicate",
                    mail_id=new_mail.message_id,
                    summary="  PAY   THE FEE ",
                    action_type="meeting",
                    due_at=due,
                    notes="Client A",
                ),
                ActionItem(
                    item_id="new-custom-duplicate",
                    mail_id=new_mail.message_id,
                    summary="submit report",
                    action_type="other",
                    due_at=due,
                ),
            ],
        )
        new_analysis.action_items.append(
            new_analysis.action_items[0].model_copy(
                update={"item_id": "other-client", "notes": "Client B"}
            )
        )
        runtime, _, _ = build_runtime({}, analysis=new_analysis, storage=storage)

        stored = await runtime.process_mail_now(new_mail)

        assert stored is not None
        assert len(stored.action_items) == 3
        item_ids = {item.notes: item.item_id for item in stored.action_items}
        assert len(set(item_ids.values())) == 3
        stored.action_items[0].completed_at = datetime.now(UTC)
        await storage.save_mail(stored)
        new_analysis.action_items.reverse()
        repeated = await runtime.process_mail_now(new_mail, force=True)
        assert repeated is not None
        assert {item.notes: item.item_id for item in repeated.action_items} == item_ids
        completed = [item.notes for item in repeated.action_items if item.completed_at is not None]
        assert completed == ["Client A"]
        assert len(await runtime.action_store.read()) == 5


class TestDailyDigest:
    async def test_digest_fires_once_after_reminder_hour(self) -> None:
        """At 08:00 (configurable) the runtime emits one digest per day with
        today/upcoming counts and the approaching items."""
        from mailflow.domain import ActionItem

        storage = FakeStorage()
        now = datetime(2026, 8, 16, 9, 0, tzinfo=UTC)  # after the 08:00 hour
        today_item = ActionItem(
            item_id="due-today",
            mail_id="",
            summary="Submit report",
            action_type="errand",
            due_at=datetime(2026, 8, 16, 15, 0, tzinfo=UTC),
            notes="",
        )
        soon_item = ActionItem(
            item_id="due-soon",
            mail_id="",
            summary="Prepare slides",
            action_type="meeting",
            due_at=datetime(2026, 8, 17, 10, 0, tzinfo=UTC),
            notes="",
        )
        await storage.save_custom_action(today_item)
        await storage.save_custom_action(soon_item)
        events = EventBus()
        config = MailFlowConfig.model_validate(
            {"general": {"workers": 1, "reminder_interval_seconds": 10, "reminder_hour": 8}}
        )
        runtime = MailFlowRuntime(
            config,
            sources={},
            pipeline=PipelineEngine([]),
            storage=storage,
            notifiers=[],
            notifier_configs=[],
            events=events,
            account_configs=[],
        )
        digests: list[dict[str, Any]] = []

        async def capture(event: str, **payload: Any) -> bool:
            digests.append(payload)
            return True

        events.subscribe("mailflow.action.digest", capture)
        assert await runtime._fire_daily_digest(now.replace(hour=7), config.general) == 0  # pyright: ignore[reportPrivateUsage]
        assert digests == []
        assert await runtime._fire_daily_digest(now, config.general) == 1  # pyright: ignore[reportPrivateUsage]
        assert len(digests) == 1
        assert digests[0]["today_count"] == 1
        assert digests[0]["upcoming_count"] == 1
        assert [i.item_id for i in digests[0]["items"]] == ["due-today", "due-soon"]
        # once per day
        assert await runtime._fire_daily_digest(now, config.general) == 0  # pyright: ignore[reportPrivateUsage]
        assert len(digests) == 1


@pytest.mark.parametrize("switch_language", [False, True])
async def test_hourly_summary_respects_closed_hours_preferences_and_delivery_receipts(
    switch_language: bool,
) -> None:
    calls: list[list[dict[str, Any]]] = []
    pushed: list[tuple[str, str]] = []

    class Router:
        async def chat(self, messages: Any, **kwargs: Any) -> Any:
            calls.append(messages)
            from mailflow.contracts import LLMCompletion

            if len(calls) == 1:
                raise TimeoutError("model unavailable")
            language = "zh-CN" if "Response language: zh-CN" in messages[1]["content"] else "en"
            return LLMCompletion(text=f"Hour briefing ({language}): one important mail.")

    class Notifier:
        fail = True

        async def push_text(self, text: str) -> None:
            pushed.append(("default", text))

        async def push_to_target(self, target: str, text: str) -> None:
            if target == "group:777" and self.fail:
                self.fail = False
                raise TimeoutError("notifier unavailable")
            pushed.append((target, text))

    storage = FakeStorage()
    mail = make_mail()
    mail.received_at = datetime(2026, 8, 16, 9, 30, tzinfo=UTC)
    for index in range(51):
        record = mail.model_copy(update={"subject": f"hour-mail-{index:03d}"})
        await storage.save_mail(MailRecord(record_id=f"hour-{index}", mail=record))
    config = MailFlowConfig.model_validate(
        {
            "general": {
                "workers": 1,
                "hourly_summary": False,
                "timezone": "Asia/Shanghai",
                "language": "zh-CN",
                "summary_language": "en",
            },
            "llms": [{"llm_id": "llm-1"}],
        }
    )
    engine = PipelineEngine([])
    engine._router = Router()  # pyright: ignore[reportPrivateUsage]
    runtime = MailFlowRuntime(
        config,
        sources={},
        pipeline=engine,
        storage=storage,
        notifiers=[Notifier()],  # type: ignore[list-item]
        notifier_configs=[
            NotifierConfig(
                notifier_id="n0",
                provider="onebot",
                options={
                    "hourly_summary": True,
                    "targets": ["group:888", "group:999", "group:777"],
                },
            )
        ],
        events=EventBus(),
        account_configs=[],
    )
    closed = datetime(2026, 8, 16, 10, 5, tzinfo=UTC)
    assert await runtime._fire_hourly_summary(closed, config.general) == 0  # pyright: ignore[reportPrivateUsage]
    config.general.hourly_summary = True
    assert (
        await runtime._fire_hourly_summary(datetime(2026, 8, 16, 9, 45, tzinfo=UTC), config.general)  # pyright: ignore[reportPrivateUsage]
        == 0
    )
    assert calls == [] and pushed == []
    await storage.set_preference("hourly_summary.chat.onebot.n0.999", "off")
    # A failed model call and then one failed destination must both be retryable.
    assert await runtime._fire_hourly_summary(closed, config.general) == 0  # pyright: ignore[reportPrivateUsage]
    assert not await storage.get_preference("hourly_summary.2026-08-16T09:00")
    assert await runtime._fire_hourly_summary(closed, config.general) == 0  # pyright: ignore[reportPrivateUsage]
    assert not await storage.get_preference("hourly_summary.2026-08-16T09:00")
    assert [target for target, _ in pushed] == ["group:888"]
    calls_before_retry = len(calls)
    if switch_language:
        config.general.language = "en"
        failures = await runtime.failed_deliveries()
        assert await runtime.retry_deliveries([row["delivery_id"] for row in failures]) == 1
    assert await runtime._fire_hourly_summary(closed, config.general) == 1  # pyright: ignore[reportPrivateUsage]
    assert [target for target, _ in pushed] == ["group:888", "group:777"]
    assert (pushed[0][1] != pushed[1][1]) == switch_language
    assert len(calls) == calls_before_retry + (2 if switch_language else 0)
    calls_before_retry = len(calls)
    prompt = "\n".join(message["content"] for call in calls[1:3] for message in call)
    assert all(prompt.count(f"hour-mail-{index:03d}") == 1 for index in range(51))
    assert "zh-CN" in prompt and "Asia/Shanghai" in prompt
    assert "2026-08-16T17:00:00+08:00" in prompt
    assert await runtime._fire_hourly_summary(closed, config.general) == 0  # pyright: ignore[reportPrivateUsage]
    assert len(calls) == calls_before_retry and len(pushed) == 2
    assert await runtime._fire_hourly_summary(closed + timedelta(hours=1), config.general) == 0  # pyright: ignore[reportPrivateUsage]
    assert await storage.get_preference("hourly_summary.2026-08-16T10:00")
    assert len(calls) == calls_before_retry


class TestFailedReanalysisKeepsPrevious:
    """A failed re-analysis must not throw away a working analysis."""

    @staticmethod
    async def _runtime_with(pipeline: Any) -> tuple[MailFlowRuntime, FakeStorage]:
        storage = FakeStorage()
        previous_mail = make_mail(subject="Original mail")
        record = MailRecord(
            record_id=previous_mail.normalized_message_id(),
            mail=previous_mail,
            auto_urgency=Urgency.URGENT,
            analysis=MailAnalysis(
                summary="Good previous summary",
                urgency=Urgency.URGENT,
                reason="previous reason",
                action_items=[
                    ActionItem(
                        item_id="m1-1",
                        mail_id="m1",
                        summary="Collect the document",
                        action_type="errand",
                        due_at=datetime(2030, 1, 1, tzinfo=UTC),
                    )
                ],
            ),
        )
        await storage.save_mail(record)
        runtime = MailFlowRuntime(
            MailFlowConfig.model_validate(
                {"storage": {"provider": "fake"}, "general": {"workers": 1}}
            ),
            sources={},
            pipeline=pipeline,
            storage=storage,
            notifiers=[],
            notifier_configs=[],
            events=EventBus(),
            account_configs=[],
        )
        return runtime, storage

    async def test_failed_run_keeps_the_previous_analysis(self) -> None:
        class FailingProcessor:
            async def process(
                self, mail: MailMessage, context: ProcessingContext
            ) -> ProcessorResult:
                raise TimeoutError("model unavailable")

        pipeline = PipelineEngine(
            [ProcessorBinding("llm-importance", "fake", FailingProcessor(), retries=0)]
        )
        runtime, storage = await self._runtime_with(pipeline)
        mail = (await storage.list_mails())[0].mail
        for _ in range(3):
            stored = await runtime.process_mail_now(mail, force=True)
            assert stored is not None
            assert stored.summary == "Good previous summary"
            assert stored.effective_urgency is Urgency.URGENT
            assert stored.analysis is not None and stored.analysis.reason == "previous reason"
            assert not stored.analysis_is_fallback
            assert stored.analysis_state == "failed"
            assert [item.summary for item in stored.action_items] == ["Collect the document"]
            assert any(note.status == "failed" for note in stored.processor_notes)
            # Include the JSON round trip used when storage is reopened.
            await storage.save_mail(MailRecord.model_validate_json(stored.model_dump_json()))

    async def test_successful_run_replaces_the_previous_analysis(self) -> None:
        class WorkingPipeline:
            router = None

            async def process(self, mail: Any, account_id: str, **kwargs: Any) -> Any:
                return (
                    MailAnalysis(summary="Fresh summary", urgency=Urgency.INFO),
                    [
                        ProcessorNote(
                            processor_id="llm-importance",
                            plugin_id="mailflow-core",
                            status="success",
                            message="ok",
                            started_at=datetime(2026, 1, 1, tzinfo=UTC),
                            finished_at=datetime(2026, 1, 1, tzinfo=UTC),
                        )
                    ],
                    "llm-1",
                    "backend",
                )

        runtime, storage = await self._runtime_with(WorkingPipeline())
        mail = (await storage.list_mails())[0].mail

        stored = await runtime.process_mail_now(mail, force=True)

        assert stored is not None
        assert stored.summary == "Fresh summary"
        assert stored.effective_urgency is Urgency.INFO
        assert [note.status for note in stored.processor_notes] == ["success"]

    async def test_first_analysis_failure_is_stored_as_a_fallback(self) -> None:
        """Without a previous analysis there is nothing to keep."""

        class FailingPipeline:
            router = None

            async def process(self, mail: Any, account_id: str, **kwargs: Any) -> Any:
                return (
                    MailAnalysis(
                        summary=mail.subject, urgency=Urgency.INFO, summary_is_fallback=True
                    ),
                    [
                        ProcessorNote(
                            processor_id="llm-importance",
                            plugin_id="mailflow-core",
                            status="failed",
                            message="failed: provider down",
                            started_at=datetime(2026, 1, 1, tzinfo=UTC),
                            finished_at=datetime(2026, 1, 1, tzinfo=UTC),
                        )
                    ],
                    "",
                    "",
                )

        runtime, storage = await self._runtime_with(FailingPipeline())
        mail = (await storage.list_mails())[0].mail
        storage.saved.clear()  # no previous record to keep

        stored = await runtime.process_mail_now(mail, force=True)

        assert stored is not None
        assert stored.analysis_is_fallback
        assert any(note.status == "failed" for note in stored.processor_notes)


class TestSourcesSurviveUnrelatedEdits:
    """An LLM/notifier edit must not tear down live source connections."""

    class _CountingSource:
        def __init__(self) -> None:
            self.closed = 0
            self.started = 0

        async def run(self, emit: Any, stop_event: Any) -> None:
            self.started += 1
            await stop_event.wait()

        async def send_reply(self, mail_id: str, draft: Any) -> None:
            return None

        async def close(self) -> None:
            self.closed += 1

    async def _runtime(self) -> tuple[MailFlowRuntime, Any]:
        account = MailAccountConfig(account_id="acct-1", provider="fake")
        source = self._CountingSource()
        runtime = MailFlowRuntime(
            MailFlowConfig.model_validate(
                {
                    "general": {"workers": 1},
                    "accounts": [{"account_id": "acct-1", "provider": "fake"}],
                }
            ),
            sources={"acct-1": source},
            pipeline=PipelineEngine([]),
            storage=FakeStorage(),
            notifiers=[],
            notifier_configs=[],
            events=EventBus(),
            account_configs=[account],
        )
        await runtime.start()
        await asyncio.sleep(0)  # let the source task enter run()
        return runtime, source

    async def test_unchanged_accounts_keep_the_same_source_instance(self) -> None:
        runtime, source = await self._runtime()
        try:
            assert source.started == 1
            same_config = runtime._config  # pyright: ignore[reportPrivateUsage]

            await runtime.reconfigure(
                config=same_config,
                sources={"acct-1": self._CountingSource()},
                pipeline=PipelineEngine([]),
                notifiers=[],
                notifier_configs=[],
            )

            # the running adapter (and its connection) is untouched
            assert source.closed == 0
            assert source.started == 1
            assert runtime._sources["acct-1"] is source  # pyright: ignore[reportPrivateUsage]
        finally:
            await runtime.stop()

    async def test_a_changed_account_restarts_the_source(self) -> None:
        runtime, source = await self._runtime()
        try:
            changed = MailFlowConfig.model_validate(
                {
                    "general": {"workers": 1},
                    "accounts": [{"account_id": "acct-1", "provider": "fake", "email": "new"}],
                }
            )

            await runtime.reconfigure(
                config=changed,
                sources={"acct-1": self._CountingSource()},
                pipeline=PipelineEngine([]),
                notifiers=[],
                notifier_configs=[],
            )

            assert source.closed == 1
        finally:
            await runtime.stop()

    async def test_plugin_changes_can_force_a_restart(self) -> None:
        runtime, source = await self._runtime()
        try:
            same_config = runtime._config  # pyright: ignore[reportPrivateUsage]

            await runtime.reconfigure(
                config=same_config,
                sources={"acct-1": self._CountingSource()},
                pipeline=PipelineEngine([]),
                notifiers=[],
                notifier_configs=[],
                restart_sources=True,
            )

            assert source.closed == 1
        finally:
            await runtime.stop()

    async def test_repeated_start_stop_and_restart_have_one_task_set(self) -> None:
        runtime, source = await self._runtime()
        first = list(runtime._tasks)  # pyright: ignore[reportPrivateUsage]
        await runtime.start()
        assert runtime._tasks == first  # pyright: ignore[reportPrivateUsage]
        await runtime.stop()
        await runtime.stop()
        assert source.closed == 1
        assert all(task.done() for task in first)
        await runtime.start()
        await asyncio.sleep(0)
        try:
            assert source.started == 2
            current = runtime._tasks  # pyright: ignore[reportPrivateUsage]
            assert len(current) == len(first)
            assert not set(current) & set(first)
        finally:
            await runtime.stop()


async def test_reload_preserves_queued_and_inflight_mail_when_queue_shrinks() -> None:
    entered = asyncio.Event()

    class BlockedProcessor(StaticProcessor):
        async def process(self, mail: MailMessage, context: ProcessingContext) -> ProcessorResult:
            entered.set()
            await asyncio.Event().wait()
            return await super().process(mail, context)

    mails = [make_mail(subject=str(index)) for index in range(4)]
    config = MailFlowConfig.model_validate(
        {
            "general": {"workers": 1, "queue_size": 4},
            "accounts": [{"account_id": "acct-1", "provider": "fake"}],
        }
    )
    storage = FakeStorage()
    runtime = MailFlowRuntime(
        config,
        sources={"acct-1": EmittingSource(mails)},
        pipeline=PipelineEngine(
            [
                ProcessorBinding(
                    processor_id="blocked",
                    plugin_id="test",
                    processor=BlockedProcessor(MailAnalysis(summary="old", urgency=Urgency.INFO)),
                )
            ]
        ),
        storage=storage,
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        account_configs=config.accounts,
    )
    await runtime.start()
    await asyncio.wait_for(entered.wait(), timeout=1)
    old_tasks = list(runtime._tasks)  # pyright: ignore[reportPrivateUsage]
    changed = config.model_copy(deep=True)
    changed.general.workers = 3
    changed.general.queue_size = 1
    changed.general.reminder_interval_seconds = 30
    try:
        await asyncio.wait_for(
            runtime.reconfigure(
                config=changed,
                sources={},
                pipeline=PipelineEngine([]),
                notifiers=[],
                notifier_configs=[],
                restart_sources=True,
            ),
            timeout=3,
        )
        assert not runtime.analysis_paused
        assert await runtime.wait_idle()
        assert len(storage.saved) == len(mails)
        assert all(record.analysis_state == "ready" for record in storage.saved)
        assert runtime._queue.maxsize == 1  # pyright: ignore[reportPrivateUsage]
        names = [task.get_name() for task in runtime._tasks]  # pyright: ignore[reportPrivateUsage]
        assert len([name for name in names if name.startswith("worker-")]) == 3
        assert names.count("reminders") == names.count("cleanup") == 1
        assert all(task.done() for task in old_tasks)
    finally:
        await runtime.stop()


@pytest.mark.parametrize("shutdown", [False, True])
async def test_analysis_is_cancelled_and_never_resumed_on_start(shutdown: bool) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    entered, manual_entered = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    class BlockedProcessor(StaticProcessor):
        async def process(self, mail: MailMessage, context: ProcessingContext) -> ProcessorResult:
            calls.append(mail.subject)
            entered.set()
            if mail.subject == "manual":
                manual_entered.set()
            await asyncio.Event().wait()
            return await super().process(mail, context)

    mails = [make_mail(subject=str(index)) for index in range(3)]
    runtime, storage, _ = build_runtime(
        {"acct-1": EmittingSource(mails)},
        accounts=[MailAccountConfig(account_id="acct-1", provider="fake")],
        workers=1,
    )
    warmup = AsyncMock()
    runtime._pipeline = PipelineEngine(  # pyright: ignore[reportPrivateUsage]
        [
            ProcessorBinding(
                "blocked",
                "fake",
                BlockedProcessor(MailAnalysis(summary="done", urgency=Urgency.INFO)),
            )
        ],
        router=SimpleNamespace(warmup=warmup),  # type: ignore[arg-type]
    )
    crashed = make_mail(subject="unfinished from an earlier session")
    manual_mail = make_mail(subject="manual")
    await storage.save_mail(
        MailRecord(
            record_id=manual_mail.normalized_message_id(),
            mail=manual_mail,
            analysis=MailAnalysis(summary="last good result", urgency=Urgency.INFO),
            manual_urgency=Urgency.IMPORTANT,
        )
    )
    await storage.save_mail(
        MailRecord(
            record_id=crashed.normalized_message_id(), mail=crashed, analysis_state="pending"
        )
    )
    await runtime.start()
    await asyncio.wait_for(entered.wait(), timeout=1)
    manual = asyncio.create_task(runtime.process_mail_now(manual_mail, force=True))
    await asyncio.wait_for(manual_entered.wait(), timeout=1)
    if shutdown:
        await asyncio.wait_for(runtime.stop(), timeout=1)
    else:
        await asyncio.wait_for(runtime.set_analysis_paused(True), timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await manual
    preserved = await storage.get_mail(manual_mail.normalized_message_id())
    assert preserved is not None and preserved.analysis_state == "ready"
    assert preserved.summary == "last good result" and preserved.manual_urgency == Urgency.IMPORTANT
    assert all(
        record.analysis_state == "cancelled"
        for record in storage.saved
        if record.record_id != preserved.record_id
    )
    assert runtime.sync_info("acct-1")["pending_count"] == 0
    assert await runtime.wait_idle()
    warmup.assert_not_awaited()
    # Enabling automatic analysis only affects future arrivals; nothing resumes.
    if not shutdown:
        await runtime.set_analysis_paused(False)
        await runtime.stop()
    calls_before = list(calls)
    await runtime.start()
    try:
        assert await runtime.wait_idle()
        assert calls == calls_before
        assert not runtime.analysis_paused
        runtime._pipeline = PipelineEngine([])  # pyright: ignore[reportPrivateUsage]
        record = await runtime.process_mail_now(mails[0], force=True)
        assert record is not None and record.analysis_state == "ready"
    finally:
        await runtime.stop()


async def test_retired_source_cannot_emit_after_reload() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    attempted = asyncio.Event()

    class ReluctantSource(EmittingSource):
        async def run(self, emit: MailEmitter, stop_event: asyncio.Event) -> None:
            entered.set()
            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                await release.wait()
                attempted.set()
                await emit(make_mail(subject="stale account"))

    source = ReluctantSource([])
    config = MailFlowConfig.model_validate(
        {"accounts": [{"account_id": "acct-1", "provider": "fake"}]}
    )
    runtime, storage, _ = build_runtime({"acct-1": source}, accounts=config.accounts)
    await runtime.start()
    await asyncio.wait_for(entered.wait(), timeout=1)
    try:
        await asyncio.wait_for(
            runtime.reconfigure(
                config=MailFlowConfig(),
                sources={},
                pipeline=PipelineEngine([]),
                notifiers=[],
                notifier_configs=[],
                restart_sources=True,
            ),
            timeout=3,
        )
        release.set()
        await asyncio.wait_for(attempted.wait(), timeout=1)
        await asyncio.sleep(0)
        assert storage.saved == []
        assert runtime.account_status("acct-1") != "running"
    finally:
        release.set()
        await runtime.stop()
