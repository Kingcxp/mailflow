"""Local fixtures shared across isolated tests and component integration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from mailflow.config import MailAccountConfig, MailFlowConfig
from mailflow.contracts import (
    LLMCompletion,
    LLMRouter,
    MailMessage,
    ReplyDraft,
    ToolCall,
)
from mailflow.domain import (
    ActionItem,
    MailAddress,
    MailRecord,
    TrashRecord,
    Urgency,
)
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine
from mailflow.registry import ComponentRegistry
from mailflow.service import MailFlowService

ADDRESS = MailAddress(name="Sender", address="sender@example.com")


class MemoryStorage:
    """Minimal in-memory storage for the reply workflow tests."""

    def __init__(self) -> None:
        self.mails: dict[str, MailRecord] = {}
        self.drafts: dict[str, ReplyDraft] = {}
        self.preferences: dict[str, str] = {}
        self.custom_actions: dict[str, ActionItem] = {}
        self.deleted: list[str] = []
        self.refresh_deleted_at_calls: list[bool] = []
        self.trashed: dict[str, MailRecord] = {}
        # deletion time per trashed record: purge_trash compares against it
        self.trash_deleted_at: dict[str, datetime] = {}

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
        updated = record.model_copy(update={"manual_urgency": urgency})
        self.mails[record_id] = updated
        return updated

    async def delete_mail(self, record_id: str, *, refresh_deleted_at: bool = False) -> None:
        # the real backends move the record to the trash; the fake does the
        # same so purge tests can assert what the user still has
        self.deleted.append(record_id)
        self.refresh_deleted_at_calls.append(refresh_deleted_at)
        record = self.mails.pop(record_id, None)
        if record is not None:
            self.trashed[record_id] = record
            self.trash_deleted_at[record_id] = datetime.now(UTC)

    async def list_trash(self) -> list[TrashRecord]:
        now = datetime.now(UTC)
        return [
            TrashRecord(
                record_id=record_id,
                mail=record.mail,
                auto_urgency=record.auto_urgency,
                manual_urgency=record.manual_urgency,
                analysis=record.analysis,
                processor_notes=record.processor_notes,
                deleted_at=now,
                expires_at=now,
            )
            for record_id, record in self.trashed.items()
        ]

    async def restore_from_trash(self, record_id: str) -> MailRecord | None:
        return None

    async def purge_trash(self, before: datetime) -> int:
        purged = [key for key, when in list(self.trash_deleted_at.items()) if when < before]
        for key in purged:
            self.trashed.pop(key, None)
            self.trash_deleted_at.pop(key, None)
        return len(purged)

    async def cleanup_mail(self, before: datetime) -> int:
        moved = 0
        for record_id in list(self.mails):
            if self.mails[record_id].received_at < before:
                await self.delete_mail(record_id)
                moved += 1
        return moved

    async def save_draft(self, draft: ReplyDraft) -> None:
        self.drafts[draft.draft_id] = draft

    async def get_draft(self, draft_id: str) -> ReplyDraft | None:
        return self.drafts.get(draft_id)

    async def delete_draft(self, draft_id: str) -> None:
        self.drafts.pop(draft_id, None)

    async def get_preference(self, key: str) -> str | None:
        return self.preferences.get(key)

    async def set_preference(self, key: str, value: str) -> None:
        self.preferences[key] = value

    async def save_custom_action(self, item: ActionItem) -> None:
        self.custom_actions[item.item_id] = item

    async def list_custom_actions(self) -> list[ActionItem]:
        return list(self.custom_actions.values())

    async def delete_custom_action(self, item_id: str) -> bool:
        return self.custom_actions.pop(item_id, None) is not None


class OrderedMemoryStorage(MemoryStorage):
    """Tools receive the same newest-first inventory as persistent storage."""

    async def list_mails(self, limit: int | None = None) -> list[MailRecord]:
        ordered = sorted(
            self.mails.values(), key=lambda record: record.mail.received_at, reverse=True
        )
        return ordered if limit is None else ordered[:limit]


class RecordingSource:
    def __init__(self) -> None:
        self.sent: list[tuple[str, ReplyDraft]] = []
        self.fail_send = False

    async def run(self, emit: Any, stop_event: Any) -> None:
        await stop_event.wait()

    async def send_reply(self, mail_id: str, draft: ReplyDraft) -> None:
        if self.fail_send:
            raise RuntimeError("provider send failed")
        self.sent.append((mail_id, draft))

    async def close(self) -> None:
        pass


def model_completion(text: str, *, tool_calls: list[ToolCall] | None = None) -> LLMCompletion:
    """One scripted model turn for the tool-loop tests."""
    return LLMCompletion(text=text, model="test-model", tool_calls=tool_calls or [])


def make_record() -> MailRecord:
    mail = MailMessage(
        message_id="m1",
        account_id="acct-1",
        subject="Hello",
        sender=ADDRESS,
        recipients=[],
        cc=[],
        date=datetime(2026, 1, 1, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        body_text="body",
        body_html="<p>body</p>",
        provider="fake",
    )
    return MailRecord(record_id="m1", mail=mail, auto_urgency=Urgency.INFO)


@pytest.fixture
async def service() -> MailFlowService:
    storage = MemoryStorage()
    await storage.save_mail(make_record())
    source = RecordingSource()
    svc = MailFlowService(
        config=MailFlowConfig(accounts=[MailAccountConfig(account_id="acct-1", provider="fake")]),
        registry=ComponentRegistry(),
        plugin_manager=cast(Any, None),
        storage=cast(Any, storage),
        sources={"acct-1": source},
        router=cast(LLMRouter, None),
        pipeline=PipelineEngine([]),
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        i18n=I18n(),
    )
    return svc
