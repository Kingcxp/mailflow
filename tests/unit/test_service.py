"""Unit tests for the service reply state machine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from mailflow.config import MailFlowConfig
from mailflow.contracts import LLMRouter, MailMessage, ReplyDraft
from mailflow.domain import (
    MailAddress,
    MailRecord,
    ReplyState,
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
        return None

    async def delete_mail(self, record_id: str) -> None:
        pass

    async def list_trash(self) -> list[TrashRecord]:
        return []

    async def restore_from_trash(self, record_id: str) -> MailRecord | None:
        return None

    async def purge_trash(self, before: datetime) -> int:
        return 0

    async def cleanup_mail(self, before: datetime) -> int:
        return 0

    async def save_draft(self, draft: ReplyDraft) -> None:
        self.drafts[draft.draft_id] = draft

    async def get_draft(self, draft_id: str) -> ReplyDraft | None:
        return self.drafts.get(draft_id)

    async def delete_draft(self, draft_id: str) -> None:
        self.drafts.pop(draft_id, None)

    async def get_preference(self, key: str) -> str | None:
        return None

    async def set_preference(self, key: str, value: str) -> None:
        pass


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
        config=MailFlowConfig(),
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


class TestReplyWorkflow:
    async def test_create_edit_prepare_confirm_happy_path(self, service: MailFlowService) -> None:
        draft = await service.create_reply("m1")
        assert draft.state is ReplyState.DRAFT
        assert draft.to.address == "sender@example.com"

        edited = await service.edit_draft(draft.draft_id, "Re: Hello", "Sounds good")
        assert edited.subject == "Re: Hello"
        assert edited.body == "Sounds good"

        prepared = await service.prepare_reply(draft.draft_id)
        assert prepared.state is ReplyState.PREPARED
        assert prepared.token is not None

        confirmed = await service.confirm_reply(draft.draft_id, prepared.token or "")
        assert confirmed.state is ReplyState.SENT
        source: RecordingSource = service.sources["acct-1"]  # type: ignore[assignment]
        assert len(source.sent) == 1
        assert source.sent[0][0] == "m1"

    async def test_confirm_without_prepare_rejected(self, service: MailFlowService) -> None:
        draft = await service.create_reply("m1")
        with pytest.raises(PermissionError):
            await service.confirm_reply(draft.draft_id, "no-token")

    async def test_wrong_token_rejected(self, service: MailFlowService) -> None:
        draft = await service.create_reply("m1")
        await service.prepare_reply(draft.draft_id)
        with pytest.raises(PermissionError):
            await service.confirm_reply(draft.draft_id, "wrong-token")

    async def test_expired_token_rejected(self, service: MailFlowService) -> None:
        draft = await service.create_reply("m1")
        await service.prepare_reply(draft.draft_id)
        stored: ReplyDraft = await service.storage.get_draft(draft.draft_id)  # type: ignore[attr-defined]
        stored.token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await service.storage.save_draft(stored)  # type: ignore[attr-defined]
        with pytest.raises(PermissionError):
            await service.confirm_reply(draft.draft_id, stored.token or "")

    async def test_confirm_consumes_token(self, service: MailFlowService) -> None:
        draft = await service.create_reply("m1")
        prepared = await service.prepare_reply(draft.draft_id)
        assert prepared.token is not None
        await service.confirm_reply(draft.draft_id, prepared.token)
        # token consumed by the first confirm -> second attempt must fail
        with pytest.raises(PermissionError):
            await service.confirm_reply(draft.draft_id, prepared.token)

    async def test_edit_after_sent_rejected(self, service: MailFlowService) -> None:
        draft = await service.create_reply("m1")
        prepared = await service.prepare_reply(draft.draft_id)
        await service.confirm_reply(draft.draft_id, prepared.token or "")
        with pytest.raises(ValueError, match="cannot edit"):
            await service.edit_draft(draft.draft_id, "x", "y")

    async def test_cancel_sent_rejected(self, service: MailFlowService) -> None:
        draft = await service.create_reply("m1")
        prepared = await service.prepare_reply(draft.draft_id)
        await service.confirm_reply(draft.draft_id, prepared.token or "")
        with pytest.raises(ValueError, match="already sent"):
            await service.cancel_reply(draft.draft_id)

    async def test_cancel_before_send(self, service: MailFlowService) -> None:
        draft = await service.create_reply("m1")
        cancelled = await service.cancel_reply(draft.draft_id)
        assert cancelled.state is ReplyState.CANCELLED
        with pytest.raises(ValueError):
            await service.prepare_reply(draft.draft_id)

    async def test_edit_invalidates_prepared_token(self, service: MailFlowService) -> None:
        draft = await service.create_reply("m1")
        prepared = await service.prepare_reply(draft.draft_id)
        assert prepared.token is not None
        await service.edit_draft(draft.draft_id, "Re: Hello", "changed")
        with pytest.raises(PermissionError):
            await service.confirm_reply(draft.draft_id, prepared.token or "")

    async def test_send_failure_reverts_to_draft(self, service: MailFlowService) -> None:
        source: RecordingSource = service.sources["acct-1"]  # type: ignore[assignment]
        source.fail_send = True
        draft = await service.create_reply("m1")
        prepared = await service.prepare_reply(draft.draft_id)
        with pytest.raises(RuntimeError, match="provider send failed"):
            await service.confirm_reply(draft.draft_id, prepared.token or "")
        stored: ReplyDraft = await service.storage.get_draft(draft.draft_id)  # type: ignore[attr-defined]
        assert stored.state is ReplyState.DRAFT
        assert stored.token is None
        # token consumed -> cannot re-confirm, no double send
        assert source.sent == []

    async def test_create_reply_unknown_mail(self, service: MailFlowService) -> None:
        with pytest.raises(KeyError):
            await service.create_reply("ghost")
