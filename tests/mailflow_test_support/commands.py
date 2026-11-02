"""Local fixtures shared across isolated tests and component integration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from mailflow.commands import CommandRouter
from mailflow.config import MailAccountConfig, MailFlowConfig
from mailflow.contracts import LLMRouter, MailMessage, ReplyDraft
from mailflow.domain import (
    ActionItem,
    MailAddress,
    MailAnalysis,
    MailRecord,
    TrashRecord,
    Urgency,
)
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine
from mailflow.plugins import PluginManager
from mailflow.registry import ComponentRegistry
from mailflow.service import MailFlowService

ADDRESS = MailAddress(name="Sender", address="sender@example.com")


def commands_service(
    config: MailFlowConfig | None = None, config_path: str | None = None
) -> MailFlowService:
    """Build a service with the given config (used by config-set tests)."""
    service = MailFlowService(
        config=config or MailFlowConfig(),
        registry=ComponentRegistry(),
        plugin_manager=PluginManager(),
        storage=cast(Any, MemoryStorage()),
        sources={},
        router=cast(LLMRouter, None),
        pipeline=PipelineEngine([]),
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        i18n=I18n(),
    )
    if config_path:
        service.config_path = Path(config_path)
    return service


class MemoryStorage:
    def __init__(self) -> None:
        self.mails: dict[str, MailRecord] = {}
        self.trash: dict[str, TrashRecord] = {}
        self.drafts: dict[str, ReplyDraft] = {}
        self.preferences: dict[str, str] = {}
        self.custom_actions: dict[str, ActionItem] = {}

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def save_mail(self, record: MailRecord) -> None:
        self.mails[record.record_id] = record

    async def get_mail(self, record_id: str) -> MailRecord | None:
        return self.mails.get(record_id)

    async def list_mails(self, limit: int | None = None) -> list[MailRecord]:
        return list(self.mails.values())[:limit]

    async def count_mails(self) -> int:
        return len(self.mails)

    async def set_manual_urgency(
        self, record_id: str, urgency: Urgency | None
    ) -> MailRecord | None:
        record = self.mails.get(record_id)
        if record is None:
            return None
        record.manual_urgency = urgency
        return record

    async def delete_mail(self, record_id: str, *, refresh_deleted_at: bool = False) -> None:
        record = self.mails.pop(record_id, None)
        if record is not None:
            self.trash[record_id] = TrashRecord(
                record_id=record_id,
                mail=record.mail,
                auto_urgency=record.auto_urgency,
                manual_urgency=record.manual_urgency,
                analysis=record.analysis,
                processor_notes=record.processor_notes,
                deleted_at=datetime.now(UTC),
                expires_at=datetime.now(UTC),
            )

    async def list_trash(self) -> list[TrashRecord]:
        return list(self.trash.values())

    async def restore_from_trash(self, record_id: str) -> MailRecord | None:
        item = self.trash.pop(record_id, None)
        if item is None:
            return None
        record = item.to_mail_record()
        self.mails[record_id] = record
        return record

    async def cleanup_mail(self, before: datetime) -> int:
        moved = 0
        for record_id in list(self.mails):
            if self.mails[record_id].received_at < before:
                self.trash[record_id] = self._to_trash(self.mails.pop(record_id))
                moved += 1
        return moved

    def _to_trash(self, record: MailRecord) -> TrashRecord:
        from mailflow.domain import TrashRecord, utcnow

        return TrashRecord(
            record_id=record.record_id,
            mail=record.mail,
            auto_urgency=record.auto_urgency,
            manual_urgency=record.manual_urgency,
            analysis=record.analysis,
            processor_notes=record.processor_notes,
            deleted_at=datetime.now(UTC),
            expires_at=utcnow(),
        )

    async def purge_trash(self, before: datetime) -> int:
        purged = 0
        for record_id in list(self.trash):
            if self.trash[record_id].deleted_at < before:
                del self.trash[record_id]
                purged += 1
        return purged

    async def save_draft(self, draft: ReplyDraft) -> None:
        self.drafts[draft.draft_id] = draft

    async def get_draft(self, draft_id: str) -> ReplyDraft | None:
        return self.drafts.get(draft_id)

    async def list_drafts(self, mail_id: str) -> list[ReplyDraft]:
        return [draft for draft in self.drafts.values() if draft.mail_id == mail_id]

    async def delete_draft(self, draft_id: str) -> None:
        self.drafts.pop(draft_id, None)

    async def get_preference(self, key: str) -> str | None:
        return self.preferences.get(key)

    async def set_preference(self, key: str, value: str) -> None:
        self.preferences[key] = value

    async def update_mail_analysis(
        self,
        record_id: str,
        *,
        urgency: Urgency | None = None,
        summary: str | None = None,
        reason: str | None = None,
    ) -> MailRecord | None:
        record = self.mails.get(record_id)
        if record is None:
            return None
        analysis = record.analysis
        if analysis is None:
            from mailflow.domain import MailAnalysis

            analysis = MailAnalysis(
                summary=summary or record.mail.subject, urgency=urgency or record.auto_urgency
            )
            record.analysis = analysis
        if urgency is not None:
            analysis.urgency = urgency
            record.auto_urgency = urgency
        if summary is not None:
            analysis.summary = summary
        if reason is not None:
            analysis.reason = reason
        return record

    async def save_custom_action(self, item: ActionItem) -> None:
        self.custom_actions[item.item_id] = item

    async def list_custom_actions(self) -> list[ActionItem]:
        return list(self.custom_actions.values())

    async def delete_custom_action(self, item_id: str) -> bool:
        return self.custom_actions.pop(item_id, None) is not None


def make_record(
    record_id: str = "m1",
    urgency: Urgency = Urgency.IMPORTANT,
    subject: str = "Exam on Friday",
    action: ActionItem | None = None,
) -> MailRecord:
    mail = MailMessage(
        message_id=record_id,
        account_id="acct-1",
        subject=subject,
        sender=ADDRESS,
        recipients=[],
        cc=[],
        date=datetime(2026, 6, 1, 8, 0, tzinfo=UTC),
        received_at=datetime(2026, 6, 1, 8, 5, tzinfo=UTC),
        body_text="Original body content",
        body_html="<p>Original body content</p>",
        provider="fake",
    )
    analysis = MailAnalysis(
        summary="Bring your student ID",
        urgency=urgency,
        reason="exam requires identification",
        reply_required=True,
        suggested_reply="I will attend.",
        action_items=[action] if action else [],
    )
    return MailRecord(record_id=record_id, mail=mail, auto_urgency=urgency, analysis=analysis)


@pytest.fixture
async def router() -> tuple[CommandRouter, MemoryStorage]:
    storage = MemoryStorage()
    await storage.save_mail(make_record())
    await storage.save_mail(make_record(record_id="m2", urgency=Urgency.AD, subject="Sale!"))
    await storage.save_mail(
        make_record(
            record_id="m3",
            urgency=Urgency.URGENT,
            subject="Exam",
            action=ActionItem(
                item_id="a1",
                mail_id="m3",
                summary="Final calculus exam",
                action_type="exam",
                due_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
                notes="Bring student ID and calculator",
            ),
        )
    )

    class NoopSource:
        async def run(self, emit: Any, stop_event: Any) -> None:
            await stop_event.wait()

        async def send_reply(self, mail_id: str, draft: ReplyDraft) -> None:
            pass

        async def close(self) -> None:
            pass

    service = MailFlowService(
        config=MailFlowConfig(accounts=[MailAccountConfig(account_id="acct-1", provider="fake")]),
        registry=ComponentRegistry(),
        plugin_manager=PluginManager(),
        storage=cast(Any, storage),
        sources={"acct-1": NoopSource()},
        router=cast(LLMRouter, None),
        pipeline=PipelineEngine([]),
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        i18n=I18n(),
    )
    commands = CommandRouter(service)
    return commands, storage
