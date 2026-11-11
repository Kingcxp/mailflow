"""Local fixtures shared across isolated tests and component integration tests."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

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
from mailflow.runtime import MailFlowRuntime

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
        # the real backends upsert by record id; replace so a re-analysis is
        # observable in the persisted state
        self.saved = [r for r in self.saved if r.record_id != record.record_id]
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
        record = await self.get_mail(record_id)
        if record is not None:
            record = record.model_copy(update={"manual_urgency": urgency})
            await self.save_mail(record)
        return record

    async def update_mail_analysis(
        self,
        record_id: str,
        *,
        urgency: Urgency | None = None,
        summary: str | None = None,
        reason: str | None = None,
    ) -> MailRecord | None:
        return None

    async def delete_mail(self, record_id: str, *, refresh_deleted_at: bool = False) -> None:
        self.saved = [r for r in self.saved if r.record_id != record_id]

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

    async def list_drafts(self, mail_id: str) -> list[ReplyDraft]:
        return []

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
            NotifierConfig(
                notifier_id=f"n{i}",
                provider="console",
                minimum_urgency=n.minimum,
                options={"send_interval_seconds": 0},
            )
            for i, n in enumerate(notifier_list)
        ],
        events=EventBus(),
        account_configs=accounts or [],
    )
    return runtime, storage, notifier_list
