"""Unit tests for the async runtime supervisor and retention scheduler."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

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
    MailAddress,
    MailAnalysis,
    MailRecord,
    TrashRecord,
    Urgency,
)
from mailflow.events import EventBus
from mailflow.pipeline import PipelineEngine, ProcessorBinding
from mailflow.runtime import MailFlowRuntime, seconds_until_next_cleanup

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
        return None

    async def set_preference(self, key: str, value: str) -> None:
        pass


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
