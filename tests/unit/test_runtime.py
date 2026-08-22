"""Unit tests for the async runtime supervisor and retention scheduler."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mailflow.config import MailAccountConfig, MailFlowConfig, NotifierConfig
from mailflow.contracts import (
    MailEmitter,
    MailMessage,
    MailProcessor,
    MailSource,
    ProcessingContext,
    ProcessorResult,
    ReplyDraft,
)
from mailflow.domain import (
    ActionItem,
    MailAddress,
    MailAnalysis,
    MailRecord,
    TrashRecord,
    Urgency,
)
from mailflow.events import EventBus
from mailflow.pipeline import PipelineEngine, ProcessorBinding
from mailflow.runtime import MailFlowRuntime, reminder_times, seconds_until_next_cleanup

ADDRESS = MailAddress(name="S", address="s@example.com")


def make_mail(account_id: str = "acct-1", subject: str = "Hi") -> MailMessage:
    return MailMessage(
        message_id=uuid.uuid4().hex[:16],
        account_id=account_id,
        subject=subject,
        sender=ADDRESS,
        recipients=[],
        cc=[],
        date=datetime(2026, 1, 1, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        body_text="body",
        body_html="<p>body</p>",
        provider="fake",
    )


class EmittingSource:
    """Emits a fixed list then waits for stop."""

    def __init__(self, mails: list[MailMessage], fail: bool = False) -> None:
        self.mails = mails
        self.fail = fail
        self.closed = False
        self.sent_replies: list[tuple[str, ReplyDraft]] = []

    async def run(self, emit: MailEmitter, stop_event: asyncio.Event) -> None:
        if self.fail:
            raise RuntimeError("provider down")
        for mail in self.mails:
            await emit(mail)
        await stop_event.wait()

    async def send_reply(self, mail_id: str, draft: ReplyDraft) -> None:
        self.sent_replies.append((mail_id, draft))

    async def close(self) -> None:
        self.closed = True


class StaticProcessor(MailProcessor):
    """Processor producing a fixed analysis for every mail."""

    processor_id = "static"

    def __init__(self, analysis: MailAnalysis) -> None:
        self.analysis = analysis

    async def process(self, mail: MailMessage, context: ProcessingContext) -> ProcessorResult:
        return ProcessorResult(analysis=self.analysis)


class FakeStorage:
    def __init__(self) -> None:
        self.saved: list[MailRecord] = []
        self.cleaned_before: datetime | None = None
        self.purged_before: datetime | None = None
        self._preferences: dict[str, str] = {}
        self.custom_actions: list[ActionItem] = []

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def save_mail(self, record: MailRecord) -> None:
        self.saved.append(record)

    async def get_mail(self, record_id: str) -> MailRecord | None:
        return next((r for r in self.saved if r.record_id == record_id), None)

    async def list_mails(self, limit: int | None = None) -> list[MailRecord]:
        return list(self.saved[:limit])

    async def count_mails(self) -> int:
        return len(self.saved)

    async def save_custom_action(self, item: ActionItem) -> None:
        self.custom_actions.append(item)

    async def list_custom_actions(self) -> list[ActionItem]:
        return list(self.custom_actions)

    async def delete_custom_action(self, item_id: str) -> bool:
        before = len(self.custom_actions)
        self.custom_actions = [i for i in self.custom_actions if i.item_id != item_id]
        return len(self.custom_actions) < before

    async def set_manual_urgency(
        self, record_id: str, urgency: Urgency | None
    ) -> MailRecord | None:
        return None

    async def delete_mail(self, record_id: str) -> None:
        pass

    async def list_trash(self) -> list[TrashRecord]:
        return []

    async def restore_from_trash(self, record_id: str) -> MailRecord | None:
        return None

    async def purge_trash(self, before: datetime) -> int:
        self.purged_before = before
        return 0

    async def cleanup_mail(self, before: datetime) -> int:
        self.cleaned_before = before
        return 0

    async def save_draft(self, draft: ReplyDraft) -> None:
        pass

    async def get_draft(self, draft_id: str) -> ReplyDraft | None:
        return None

    async def delete_draft(self, draft_id: str) -> None:
        pass

    async def get_preference(self, key: str) -> str | None:
        return self._preferences.get(key)

    async def set_preference(self, key: str, value: str) -> None:
        self._preferences[key] = value


class RecordingNotifier:
    def __init__(self, minimum: Urgency = Urgency.INFO) -> None:
        self.minimum = minimum
        self.notified: list[MailRecord] = []

    async def notify(self, record: MailRecord) -> None:
        self.notified.append(record)


def build_runtime(
    sources: dict[str, MailSource],
    analysis: MailAnalysis | None = None,
    accounts: list[MailAccountConfig] | None = None,
    notifiers: list[RecordingNotifier] | None = None,
    storage: FakeStorage | None = None,
    **general: object,
) -> tuple[MailFlowRuntime, FakeStorage, list[RecordingNotifier]]:
    config = MailFlowConfig.model_validate({"general": dict(general)})
    storage = storage or FakeStorage()
    processor = StaticProcessor(analysis or MailAnalysis(summary="s", urgency=Urgency.INFO))
    engine = PipelineEngine(
        [ProcessorBinding(processor_id="p", plugin_id="t", processor=processor)]
    )
    notifier_list = notifiers if notifiers is not None else [RecordingNotifier()]
    runtime = MailFlowRuntime(
        config,
        sources=sources,
        pipeline=engine,
        storage=storage,
        notifiers=[n for n in notifier_list],  # type: ignore[arg-type]
        notifier_configs=[
            NotifierConfig(notifier_id=f"n{i}", provider="console", minimum_urgency=n.minimum)
            for i, n in enumerate(notifier_list)
        ],
        events=EventBus(),
        account_configs=accounts or [],
    )
    return runtime, storage, notifier_list


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
    async def test_mail_flows_source_to_storage(self) -> None:
        analysis = MailAnalysis(summary="analyzed", urgency=Urgency.URGENT)
        source = EmittingSource([make_mail()])
        runtime, storage, notifiers = build_runtime(
            {"acct-1": source},
            analysis=analysis,
            workers=2,
            accounts=[MailAccountConfig(account_id="acct-1", provider="fake")],
        )
        await runtime.start()
        await asyncio.sleep(0.3)
        await runtime.stop()
        assert len(storage.saved) == 1
        record = storage.saved[0]
        assert record.analysis is not None
        assert record.analysis.summary == "analyzed"
        assert record.effective_urgency is Urgency.URGENT
        assert len(notifiers[0].notified) == 1
        assert source.closed is True

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
        await runtime.run_cleanup()
        assert storage.cleaned_before is not None
        assert storage.purged_before is not None
        expected_retention = datetime.now() - timedelta(days=30)
        expected_trash = datetime.now() - timedelta(days=7)
        assert abs((storage.cleaned_before - expected_retention).total_seconds()) < 60
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
        await asyncio.sleep(1.2)  # 2 retries with backoff
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

        async def capture(event: str, **payload: Any) -> None:
            reminders.append(payload)

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

        async def capture(event: str, **payload: Any) -> None:
            reminders.append(payload)

        events.subscribe("mailflow.action.reminder", capture)
        fired = await runtime.run_reminder_tick()
        # both windows whose scheduled time already passed fire (catch-up);
        # before the configured reminder hour only day_of has passed, after
        # it both early and day_of have — derive the expectation from the
        # clock instead of assuming a wall-clock range
        windows = dict(
            reminder_times(
                item,
                runtime._config.general.timezone,  # pyright: ignore[reportPrivateUsage]
                days_before=runtime._config.general.reminder_days_before,  # pyright: ignore[reportPrivateUsage]
                hour=runtime._config.general.reminder_hour,  # pyright: ignore[reportPrivateUsage]
                minute=runtime._config.general.reminder_minute,  # pyright: ignore[reportPrivateUsage]
            )
        )
        due_kinds = {kind for when, kind in windows.items() if when <= datetime.now(UTC)}
        assert fired == len(due_kinds)
        assert {r["kind"] for r in reminders} == due_kinds

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

    async def test_reminder_state_persisted_across_restart(self) -> None:
        """A fired reminder stays fired after the service restarts."""
        from mailflow.domain import ActionItem

        item = ActionItem(
            item_id="a4",
            mail_id="r1",
            summary="Due soon",
            action_type="other",
            due_at=datetime.now(UTC) + timedelta(days=2),
        )
        storage = FakeStorage()
        runtime, _ = await self._runtime_with_item(item, storage=storage)
        assert await runtime.run_reminder_tick() == 1
        # a fresh runtime on the same storage must not re-fire
        fresh_config = MailFlowConfig.model_validate(
            {"general": {"reminder_hour": 0, "reminder_minute": 0}}
        )
        fresh = MailFlowRuntime(
            fresh_config,
            sources={},
            pipeline=PipelineEngine([]),
            storage=storage,
            notifiers=[],
            notifier_configs=[],
            events=EventBus(),
            account_configs=[],
        )
        assert await fresh.run_reminder_tick() == 0


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

        async def capture(event: str, **payload: Any) -> None:
            reminders.append(payload)

        events.subscribe("mailflow.action.reminder", capture)
        fired = await runtime.run_reminder_tick()
        assert fired == 1
        assert reminders[0]["item"].item_id == "user-1"
        assert reminders[0]["record"] is None  # no source mail
        assert await runtime.run_reminder_tick() == 0
        assert len(reminders) == 1


class TestDeduplication:
    async def test_duplicate_mail_across_accounts_stored_once(self) -> None:
        """Forwarded copies of the same mail (same message id from two
        accounts) are processed, stored and notified exactly once."""
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
        assert len(storage.saved) == 1
        assert len(notifiers[0].notified) == 1

    async def test_same_account_refetch_is_skipped(self) -> None:
        """Re-fetching an already stored mail does not reprocess it."""
        analysis = MailAnalysis(summary="s", urgency=Urgency.INFO)
        mail = make_mail()
        runtime, storage, notifiers = build_runtime(
            {"acct-1": EmittingSource([mail])},
            analysis=analysis,
            workers=2,
            accounts=[MailAccountConfig(account_id="acct-1", provider="fake")],
            notifiers=[RecordingNotifier()],
        )
        # pre-seed the stored copy, then let the source emit the same mail again
        from mailflow.domain import MailRecord

        await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))
        await runtime.start()
        await asyncio.sleep(0.3)
        await runtime.stop()
        assert len(storage.saved) == 1
        assert notifiers[0].notified == []


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

        async def capture(event: str, **payload: Any) -> None:
            digests.append(payload)

        events.subscribe("mailflow.action.digest", capture)
        assert await runtime._fire_daily_digest(now, config.general) == 1  # pyright: ignore[reportPrivateUsage]
        assert len(digests) == 1
        assert digests[0]["today_count"] == 1
        assert digests[0]["upcoming_count"] == 1
        assert [i.item_id for i in digests[0]["items"]] == ["due-today", "due-soon"]
        # once per day
        assert await runtime._fire_daily_digest(now, config.general) == 0  # pyright: ignore[reportPrivateUsage]
        assert len(digests) == 1

    async def test_digest_before_reminder_hour_silent(self) -> None:
        from mailflow.domain import ActionItem

        storage = FakeStorage()
        early = datetime(2026, 8, 16, 7, 0, tzinfo=UTC)  # before 08:00
        await storage.save_custom_action(
            ActionItem(
                item_id="a",
                mail_id="",
                summary="X",
                action_type="errand",
                due_at=datetime(2026, 8, 16, 15, 0, tzinfo=UTC),
                notes="",
            )
        )
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
        assert await runtime._fire_daily_digest(early, config.general) == 0  # pyright: ignore[reportPrivateUsage]
