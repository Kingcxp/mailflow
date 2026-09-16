"""Unit tests for the service reply state machine."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from mailflow.config import LLMConfig, MailFlowConfig
from mailflow.contracts import LLMRouter, MailMessage, ProcessorResult, ReplyDraft
from mailflow.domain import (
    ActionItem,
    ActionOrigin,
    MailAddress,
    MailAnalysis,
    MailRecord,
    ProcessorNote,
    ReplyState,
    SeminarCandidate,
    SeminarStatus,
    SmartActionIntent,
    TrashRecord,
    Urgency,
    parse_urgency,
)
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine, ProcessorBinding
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
        return self.preferences.get(key)

    async def set_preference(self, key: str, value: str) -> None:
        self.preferences[key] = value

    async def save_custom_action(self, item: ActionItem) -> None:
        self.custom_actions[item.item_id] = item

    async def list_custom_actions(self) -> list[ActionItem]:
        return list(self.custom_actions.values())

    async def delete_custom_action(self, item_id: str) -> bool:
        return self.custom_actions.pop(item_id, None) is not None


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


def _reply(text: str) -> Any:
    """Minimal LLMCompletion stand-in the smart-action routers return."""

    class Reply:
        def __init__(self, value: str) -> None:
            self.text = value

    return Reply(text)


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

    async def test_concurrent_confirms_send_once(self, service: MailFlowService) -> None:
        """Two confirms racing with the same token: the per-draft lock makes
        the second re-read the persisted SENT state and fail — one send."""
        import asyncio

        source: RecordingSource = service.sources["acct-1"]  # type: ignore[assignment]
        draft = await service.create_reply("m1")
        prepared = await service.prepare_reply(draft.draft_id)
        token = prepared.token or ""
        results = await asyncio.gather(
            service.confirm_reply(draft.draft_id, token),
            service.confirm_reply(draft.draft_id, token),
            return_exceptions=True,
        )
        succeeded = [r for r in results if not isinstance(r, BaseException)]
        assert len(succeeded) == 1
        assert len(source.sent) == 1

    async def test_create_reply_unknown_mail(self, service: MailFlowService) -> None:
        with pytest.raises(KeyError):
            await service.create_reply("ghost")


class HistorySource(RecordingSource):
    """A source that also implements the optional history capability."""

    def __init__(self, mails: list[MailMessage]) -> None:
        super().__init__()
        self._mails = list(mails)
        self.history_calls: list[tuple[int, int]] = []

    async def fetch_history(self, limit: int = 50, offset: int = 0) -> list[MailMessage]:
        self.history_calls.append((limit, offset))
        newest = sorted(self._mails, key=lambda mail: mail.received_at, reverse=True)
        return newest[offset : offset + limit]


def make_mail(message_id: str, *, minute: int) -> MailMessage:
    return MailMessage(
        message_id=message_id,
        account_id="acct-1",
        subject=f"Subject {message_id}",
        sender=ADDRESS,
        recipients=[],
        cc=[],
        date=datetime(2026, 1, 1, 9, minute, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, 9, minute, tzinfo=UTC),
        body_text="body",
        provider="fake",
    )


class TestMailboxHistory:
    """Browsing already-received mail and processing a user-picked subset."""

    def _service(self, source: Any) -> MailFlowService:
        storage = MemoryStorage()
        return MailFlowService(
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

    async def test_history_accounts_lists_only_capable_sources(self) -> None:
        capable = self._service(HistorySource([make_mail("h1", minute=0)]))
        assert capable.history_accounts() == ["acct-1"]
        plain = self._service(RecordingSource())
        assert plain.history_accounts() == []

    async def test_fetch_history_paginates_newest_first(self) -> None:
        mails = [make_mail(f"h{i}", minute=i) for i in range(5)]
        source = HistorySource(mails)
        service = self._service(source)
        page = await service.fetch_history("acct-1", limit=2)
        assert [m.message_id for m in page] == ["h4", "h3"]
        assert [m.message_id for m in await service.fetch_history("acct-1", limit=2, offset=2)] == [
            "h2",
            "h1",
        ]
        assert source.history_calls == [(2, 0), (2, 2)]

    async def test_fetch_history_rejects_unknown_and_incapable(self) -> None:
        service = self._service(RecordingSource())
        with pytest.raises(KeyError):
            await service.fetch_history("ghost")
        with pytest.raises(NotImplementedError):
            await service.fetch_history("acct-1")

    async def test_process_mail_stores_and_dedups(self) -> None:
        mail = make_mail("h1", minute=0)
        service = self._service(HistorySource([mail]))
        assert await service.is_mail_known(mail) is False

        record = await service.process_mail(mail)
        assert record is not None
        assert record.record_id == mail.normalized_message_id()
        # the fallback-summary guarantee holds for on-demand processing too
        assert record.summary == "Subject h1"
        assert await service.is_mail_known(mail) is True

        # selecting the same mail again is a no-op, not a duplicate record
        assert await service.process_mail(mail) is None
        assert len(await service.list_mails()) == 1


class TestSmartSearch:
    """The LLM finder must return ranked, trustworthy, usable results."""

    def _service(self, router: Any) -> MailFlowService:
        config = MailFlowConfig()
        config.llms = [LLMConfig(llm_id="llm-1")]
        return MailFlowService(
            config=config,
            registry=ComponentRegistry(),
            plugin_manager=cast(Any, None),
            storage=cast(Any, MemoryStorage()),
            sources={},
            router=cast(LLMRouter, router),
            pipeline=PipelineEngine([]),
            notifiers=[],
            notifier_configs=[],
            events=EventBus(),
            i18n=I18n(),
        )

    @staticmethod
    def _reply(text: str) -> Any:
        class Reply:
            def __init__(self, value: str) -> None:
                self.text = value

        return Reply(text)

    async def test_fenced_scored_reply_is_parsed(self) -> None:
        class FencedRouter:
            def __init__(self) -> None:
                self.calls = 0

            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                self.calls += 1
                if messages[-1]["content"].startswith("Reply with"):
                    return TestSmartSearch._reply("ok")
                return TestSmartSearch._reply('```json\n[{"id": "m1", "relevance": 88}]\n```')

        service = self._service(FencedRouter())
        storage = cast(Any, service.storage)
        for message_id, minute in (("m1", 20), ("m2", 10)):
            mail = make_mail(message_id, minute=minute)
            await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))
        stages: list[tuple[str, int, int, Any]] = []

        def _progress(stage: str, done: int, total: int, detail: Any) -> None:
            stages.append((stage, done, total, detail))

        result = await service.smart_search("the fee notice", progress=_progress)

        assert [record.record_id for record in result.records] == ["m1"]
        assert result.is_complete
        assert result.total_mails == 2
        assert stages[0][:3] == ("warmup", 0, 1)
        assert stages[-1][:3] == ("match", 2, 2)

    async def test_prose_wrapped_legacy_candidate_refs_are_parsed(self) -> None:
        class ProseRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                if messages[-1]["content"].startswith("Reply with"):
                    return TestSmartSearch._reply("ok")
                return TestSmartSearch._reply('Matching candidate:\n["m1"]\nDone.')

        service = self._service(ProseRouter())
        storage = cast(Any, service.storage)
        for message_id, minute in (("m1", 10), ("m2", 20)):
            mail = make_mail(message_id, minute=minute)
            await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))

        result = await service.smart_search("anything about m2")

        # Candidate m1 is the newest mail in this batch, not a raw record id.
        assert [record.record_id for record in result.records] == ["m2"]

    async def test_candidate_refs_are_case_insensitive_and_hide_record_ids(self) -> None:
        opaque_record_id = "MixedCase-Opaque-Record-ID@example.test"

        class AliasRouter:
            def __init__(self) -> None:
                self.listing = ""

            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                user = messages[-1]["content"]
                if user.startswith("Reply with"):
                    return TestSmartSearch._reply("ok")
                self.listing = user
                return TestSmartSearch._reply('[{"id": "M1", "relevance": 93}]')

        router = AliasRouter()
        service = self._service(router)
        storage = cast(Any, service.storage)
        mail = make_mail("source-message", minute=10).model_copy(
            update={"subject": "Campus event invitation"}
        )
        await storage.save_mail(MailRecord(record_id=opaque_record_id, mail=mail))

        result = await service.smart_search("the event invitation")

        assert [record.record_id for record in result.records] == [opaque_record_id]
        assert opaque_record_id not in router.listing

    async def test_multilingual_candidate_is_evaluated_without_prefiltering(self) -> None:
        class BilingualRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                user = messages[-1]["content"]
                if user.startswith("Reply with"):
                    return TestSmartSearch._reply("ok")
                assert "线上研讨会通知" in user
                return TestSmartSearch._reply('[{"id": "m1", "relevance": 100}]')

        service = self._service(BilingualRouter())
        storage = cast(Any, service.storage)
        mail = make_mail("m1", minute=20).model_copy(update={"subject": "线上研讨会通知"})
        await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))
        other = make_mail("m2", minute=10)
        await storage.save_mail(MailRecord(record_id=other.normalized_message_id(), mail=other))

        result = await service.smart_search("我需要参加线上研讨会")

        assert [record.record_id for record in result.records] == ["m1"]

    async def test_scored_matches_rank_across_batches(self) -> None:
        """A highly relevant older result must precede a newer weak match."""

        class RankingRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                user = messages[-1]["content"]
                if user.startswith("Reply with"):
                    return TestSmartSearch._reply("ok")
                if "Subject m15" in user:
                    return TestSmartSearch._reply('[{"id": "m1", "relevance": 60}]')
                return TestSmartSearch._reply('[{"id": "m1", "relevance": 95}]')

        service = self._service(RankingRouter())
        storage = cast(Any, service.storage)
        for index in range(16):
            mail = make_mail(f"m{index}", minute=index)
            await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))

        result = await service.smart_search("event details")

        assert [record.record_id for record in result.records] == ["m0", "m15"]
        assert result.is_complete

    async def test_low_scored_candidates_are_not_returned(self) -> None:
        """Scores below the published floor are model non-matches, not results."""

        class PrecisionRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                if messages[-1]["content"].startswith("Reply with"):
                    return TestSmartSearch._reply("ok")
                return TestSmartSearch._reply(
                    '[{"id": "m1", "relevance": 30}, {"id": "m2", "relevance": 40}]'
                )

        service = self._service(PrecisionRouter())
        storage = cast(Any, service.storage)
        for message_id, minute in (("m1", 20), ("m2", 10)):
            mail = make_mail(message_id, minute=minute)
            await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))

        result = await service.smart_search("the student ID invitation")

        assert [record.record_id for record in result.records] == ["m2"]

    async def test_malformed_batch_is_retried_then_reported_incomplete(self) -> None:
        class BadRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                if messages[-1]["content"].startswith("Reply with"):
                    return TestSmartSearch._reply("ok")
                return TestSmartSearch._reply("I could not find anything, sorry!")

        service = self._service(BadRouter())
        storage = cast(Any, service.storage)
        for message_id in ("m1", "m2"):
            mail = make_mail(message_id, minute=10)
            await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))
        stages: list[tuple[str, int, int, Any]] = []

        def _progress(stage: str, done: int, total: int, detail: Any) -> None:
            stages.append((stage, done, total, detail))

        result = await service.smart_search("receipts", progress=_progress)

        assert result.records == []
        assert result.failed_mails == 2
        assert result.failed_batches == 1
        assert not result.is_complete
        assert stages[-1][3][0] == "smart_batch_unreadable"

    async def test_failed_batch_preserves_completed_matches(self) -> None:
        class PartiallyFailingRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                user = messages[-1]["content"]
                if user.startswith("Reply with"):
                    return TestSmartSearch._reply("ok")
                if "Subject m15" in user:
                    return TestSmartSearch._reply('[{"id": "m1", "relevance": 90}]')
                raise TimeoutError("configured endpoint timed out")

        service = self._service(PartiallyFailingRouter())
        storage = cast(Any, service.storage)
        for index in range(16):
            mail = make_mail(f"m{index}", minute=index)
            await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))
        stages: list[tuple[str, int, int, Any]] = []

        def _progress(stage: str, done: int, total: int, detail: Any) -> None:
            stages.append((stage, done, total, detail))

        result = await service.smart_search("event details", progress=_progress)

        assert [record.record_id for record in result.records] == ["m15"]
        assert result.failed_mails == 1
        assert result.failed_batches == 1
        assert not result.is_complete
        assert any(detail[0] == "smart_batch_failed" for *_rest, detail in stages)

    async def test_empty_mailbox_does_not_call_the_llm(self) -> None:
        class NoCallRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                raise AssertionError("an empty mailbox does not need an LLM")

        result = await self._service(NoCallRouter()).smart_search("anything")

        assert result.records == []
        assert result.total_mails == 0
        assert result.is_complete


class TestSeminarDiscovery:
    """Seminar discovery must remain review-only, timezone-safe and idempotent."""

    @staticmethod
    def make_service(router: Any) -> MailFlowService:
        config = MailFlowConfig()
        config.llms = [LLMConfig(llm_id="llm-1")]
        return MailFlowService(
            config=config,
            registry=ComponentRegistry(),
            plugin_manager=cast(Any, None),
            storage=cast(Any, MemoryStorage()),
            sources={},
            router=cast(LLMRouter, router),
            pipeline=PipelineEngine([]),
            notifiers=[],
            notifier_configs=[],
            events=EventBus(),
            i18n=I18n(),
        )

    async def test_discovers_review_only_candidate_and_imports_once(self) -> None:
        opaque_record_id = "opaque-record-id@example.test"

        class SeminarRouter:
            def __init__(self) -> None:
                self.listing = ""
                self.calls = 0

            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                self.calls += 1
                self.listing = messages[-1]["content"]
                title = "Research colloquium" if self.calls == 1 else "Reworded colloquium"
                return _reply(
                    "["
                    f'{{"id":"m1","title":"{title}",'
                    '"starts_at":"2030-10-15T14:00:00+08:00",'
                    '"ends_at":"2030-10-15T15:30:00+08:00",'
                    '"timezone":"Asia/Shanghai","location":"Room 201",'
                    '"url":"https://events.example.test/seminar",'
                    '"description":"Guest lecture",'
                    '"confidence":93,"evidence":"15 October, Room 201"}'
                    "]"
                )

        router = SeminarRouter()
        service = self.make_service(router)
        storage = cast(Any, service.storage)
        mail = make_mail("source-message", minute=10).model_copy(
            update={"subject": "Research seminar invitation"}
        )
        await storage.save_mail(MailRecord(record_id=opaque_record_id, mail=mail))
        stages: list[tuple[str, int, int, Any]] = []

        def _progress(stage: str, done: int, total: int, detail: Any) -> None:
            stages.append((stage, done, total, detail))

        result = await service.discover_seminars(progress=_progress)

        assert opaque_record_id not in router.listing
        assert result.total_mails == 1
        assert result.evaluated_mails == 1
        assert result.failed_mails == 0
        assert result.is_complete
        assert stages[0][:3] == ("scan", 0, 1)
        assert stages[-1][:3] == ("scan", 1, 1)
        assert await service.list_actions() == []
        candidate = result.candidates[0]
        assert candidate.mail_id == opaque_record_id
        assert candidate.starts_at == datetime(2030, 10, 15, 6, tzinfo=UTC)
        assert candidate.status is SeminarStatus.PENDING

        first = await service.import_seminar(candidate.candidate_id)
        second = await service.import_seminar(candidate.candidate_id)

        assert first.item_id == candidate.candidate_id
        assert second == first
        assert first.origin is ActionOrigin.SEMINAR
        assert first.due_end == datetime(2030, 10, 15, 7, 30, tzinfo=UTC)
        assert len(await storage.list_custom_actions()) == 1
        saved = await service.list_seminar_candidates(include_resolved=True)
        assert saved[0].status is SeminarStatus.IMPORTED
        assert (await service.discover_seminars()).candidates == []

    async def test_timed_candidate_deduplicates_when_title_changes(self) -> None:
        class RewordingRouter:
            def __init__(self) -> None:
                self.calls = 0

            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                self.calls += 1
                title = "Original seminar" if self.calls == 1 else "Reworded seminar"
                return _reply(
                    '[{"id":"m1",'
                    f'"title":"{title}",'
                    '"starts_at":"2030-10-15T14:00:00+08:00",'
                    '"timezone":"Asia/Shanghai"}]'
                )

        service = self.make_service(RewordingRouter())
        storage = cast(Any, service.storage)
        mail = make_mail("m1", minute=10)
        await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))

        first = (await service.discover_seminars()).candidates[0]
        second = (await service.discover_seminars()).candidates[0]

        assert second.candidate_id == first.candidate_id
        assert second.title == "Reworded seminar"
        assert len(await service.list_seminar_candidates(include_resolved=True)) == 1

    async def test_malformed_batch_is_retried_and_reported_incomplete(self) -> None:
        class MalformedRouter:
            def __init__(self) -> None:
                self.calls = 0

            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                self.calls += 1
                return _reply("I found a seminar but cannot format it.")

        router = MalformedRouter()
        service = self.make_service(router)
        storage = cast(Any, service.storage)
        mail = make_mail("m1", minute=10)
        await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))
        stages: list[tuple[str, int, int, Any]] = []

        def _progress(stage: str, done: int, total: int, detail: Any) -> None:
            stages.append((stage, done, total, detail))

        result = await service.discover_seminars(progress=_progress)

        assert router.calls == 2
        assert result.candidates == []
        assert result.evaluated_mails == 0
        assert result.failed_mails == 1
        assert result.failed_batches == 1
        assert not result.is_complete
        assert stages[-1][3][0] == "seminar_batch_unreadable"

    async def test_expired_candidate_requires_future_edited_confirmation(self) -> None:
        class PastSeminarRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                return _reply(
                    '[{"id":"m1","title":"Past seminar",'
                    '"starts_at":"2020-01-01T10:00:00+08:00",'
                    '"timezone":"Asia/Shanghai"}]'
                )

        service = self.make_service(PastSeminarRouter())
        storage = cast(Any, service.storage)
        mail = make_mail("m1", minute=10)
        await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))
        candidate = (await service.discover_seminars()).candidates[0]

        assert candidate.status is SeminarStatus.EXPIRED
        with pytest.raises(ValueError):
            await service.import_seminar(candidate.candidate_id)

        imported = await service.import_seminar(
            candidate.candidate_id,
            starts_at=datetime(2030, 2, 1, 10, 0),
            timezone="Asia/Shanghai",
        )

        assert imported.due_at == datetime(2030, 2, 1, 2, 0, tzinfo=UTC)

    async def test_rejected_candidate_remains_hidden_after_repeat_scan(self) -> None:
        class SeminarRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                return _reply(
                    '[{"id":"m1","title":"Seminar",'
                    '"starts_at":"2030-02-01T10:00:00+08:00",'
                    '"timezone":"Asia/Shanghai"}]'
                )

        service = self.make_service(SeminarRouter())
        storage = cast(Any, service.storage)
        mail = make_mail("m1", minute=10)
        await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))
        candidate = (await service.discover_seminars()).candidates[0]

        assert await service.reject_seminar(candidate.candidate_id)
        result = await service.discover_seminars()

        assert result.candidates == []
        saved = await service.list_seminar_candidates(include_resolved=True)
        assert saved[0].status is SeminarStatus.REJECTED


class TestSmartAction:
    """One instruction routes to filtering or to the matching operation."""

    @staticmethod
    def make_service(router: Any) -> MailFlowService:
        config = MailFlowConfig()
        config.llms = [LLMConfig(llm_id="llm-1")]
        return MailFlowService(
            config=config,
            registry=ComponentRegistry(),
            plugin_manager=cast(Any, None),
            storage=cast(Any, MemoryStorage()),
            sources={},
            router=cast(LLMRouter, router),
            pipeline=PipelineEngine([]),
            notifiers=[],
            notifier_configs=[],
            events=EventBus(),
            i18n=I18n(),
        )

    class _RoutingRouter:
        """Answers each phase by its system prompt, like a real endpoint."""

        def __init__(self, intent: str, event_json: str) -> None:
            self.intent = intent
            self.event_json = event_json
            self.phases: list[str] = []

        async def chat(self, messages: Any, **kwargs: Any) -> Any:
            system = str(messages[0]["content"])
            if system.startswith("You route one free-form"):
                self.phases.append("intent")
                return _reply(f'{{"intent":"{self.intent}"}}')
            if "smart mail finder" in system:
                self.phases.append("match")
                return _reply('[{"id":"m1","relevance":91}]')
            if system.startswith("You identify optional academic seminars"):
                self.phases.append("extract")
                return _reply(self.event_json)
            self.phases.append("warmup")
            return _reply("ok")

    async def test_search_intent_returns_ranked_matches_only(self) -> None:
        router = self._RoutingRouter("search", "[]")
        service = self.make_service(router)
        storage = cast(Any, service.storage)
        mail = make_mail("m1", minute=10)
        await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))

        result = await service.smart_action("list the registration mails")

        assert result.intent is SmartActionIntent.SEARCH
        assert [record.record_id for record in result.records] == [mail.normalized_message_id()]
        assert result.scheduled == [] and result.needs_review == []
        assert "extract" not in router.phases
        assert await storage.list_custom_actions() == []

    async def test_schedule_intent_adds_a_timed_event_to_the_schedule(self) -> None:
        event = (
            '[{"id":"m1","title":"Research colloquium",'
            '"starts_at":"2030-10-15T14:00:00+08:00",'
            '"timezone":"Asia/Shanghai","location":"Room 201",'
            '"confidence":93,"evidence":"15 October, Room 201"}]'
        )
        router = self._RoutingRouter("schedule_seminar", event)
        service = self.make_service(router)
        storage = cast(Any, service.storage)
        mail = make_mail("m1", minute=10)
        await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))

        result = await service.smart_action("add the seminars to my schedule")

        assert result.intent is SmartActionIntent.SCHEDULE_SEMINAR
        assert [item.summary for item in result.scheduled] == ["Research colloquium"]
        assert result.needs_review == []
        # the entry is a normal, deletable schedule item linked to its mail
        saved = await storage.list_custom_actions()
        assert len(saved) == 1
        assert saved[0].origin is ActionOrigin.SEMINAR
        assert saved[0].mail_id == mail.normalized_message_id()
        assert saved[0].due_at == datetime(2030, 10, 15, 6, tzinfo=UTC)

    async def test_schedule_intent_leaves_untimed_events_for_review(self) -> None:
        class UntimedRouter(TestSmartAction._RoutingRouter):
            def __init__(self) -> None:
                super().__init__(
                    "schedule_seminar",
                    '[{"id":"m1","title":"Open day","confidence":80}]',
                )

        service = self.make_service(UntimedRouter())
        storage = cast(Any, service.storage)
        mail = make_mail("m1", minute=10)
        await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))

        result = await service.smart_action("put the open days into my calendar")

        assert result.scheduled == []
        assert [candidate.title for candidate in result.needs_review] == ["Open day"]
        assert await storage.list_custom_actions() == []

    async def test_schedule_intent_ignores_proposals_from_other_mail(self) -> None:
        event = (
            '[{"id":"m1","title":"Matched seminar",'
            '"starts_at":"2030-10-15T14:00:00+08:00","timezone":"Asia/Shanghai"}]'
        )
        router = self._RoutingRouter("schedule_seminar", event)
        service = self.make_service(router)
        storage = cast(Any, service.storage)
        target = make_mail("matched", minute=10)
        await storage.save_mail(MailRecord(record_id=target.normalized_message_id(), mail=target))
        # an earlier scan's proposal for mail outside this instruction's set
        unrelated = SeminarCandidate(
            candidate_id="seminar-other",
            mail_id="earlier-scan@example.test",
            title="Unrelated seminar",
            starts_at=datetime(2030, 9, 1, 4, tzinfo=UTC),
            timezone="UTC",
        )
        await storage.set_preference(
            "seminars.candidates",
            json.dumps([unrelated.model_dump(mode="json")]),
        )

        result = await service.smart_action("schedule the seminars")

        assert [item.summary for item in result.scheduled] == ["Matched seminar"]
        saved = await storage.list_custom_actions()
        assert [item.summary for item in saved] == ["Matched seminar"]

    async def test_instruction_without_llm_reports_the_missing_model(self) -> None:
        service = self.make_service(TestSmartAction._RoutingRouter("search", "[]"))
        service.config.llms = []
        with pytest.raises(RuntimeError):
            await service.smart_action("find the exam mails")


def _expired_record(
    record_id: str,
    *,
    urgency: Urgency,
    age_hours: float = 48.0,
    due_in_hours: float | None = None,
    manual: Urgency | None = None,
) -> MailRecord:
    """One analyzed record for the expired-mail rule tests."""
    now = datetime.now(UTC)
    mail = make_mail(record_id, minute=10).model_copy(
        update={"received_at": now - timedelta(hours=age_hours)}
    )
    items = (
        [
            ActionItem(
                item_id=f"{record_id}-1",
                mail_id=record_id,
                summary="Deadline",
                action_type="errand",
                due_at=now + timedelta(hours=due_in_hours),
            )
        ]
        if due_in_hours is not None
        else []
    )
    return MailRecord(
        record_id=record_id,
        mail=mail,
        auto_urgency=urgency,
        manual_urgency=manual,
        analysis=MailAnalysis(summary="s", urgency=urgency, action_items=items),
    )


class TestExpiredMail:
    """Clearing expired mail must never touch mail that still matters."""

    @staticmethod
    def make_service() -> MailFlowService:
        config = MailFlowConfig()
        config.llms = [LLMConfig(llm_id="llm-1")]
        return MailFlowService(
            config=config,
            registry=ComponentRegistry(),
            plugin_manager=cast(Any, None),
            storage=cast(Any, MemoryStorage()),
            sources={},
            router=cast(LLMRouter, None),
            pipeline=PipelineEngine([]),
            notifiers=[],
            notifier_configs=[],
            events=EventBus(),
            i18n=I18n(),
        )

    async def test_purges_old_ads_and_past_info_only(self) -> None:
        service = self.make_service()
        storage = cast(Any, service.storage)
        await storage.save_mail(_expired_record("old-ad", urgency=Urgency.AD))
        await storage.save_mail(_expired_record("past-info", urgency=Urgency.INFO, due_in_hours=-5))
        await storage.save_mail(_expired_record("fresh-ad", urgency=Urgency.AD, age_hours=1))
        await storage.save_mail(
            _expired_record("future-info", urgency=Urgency.INFO, due_in_hours=48)
        )
        await storage.save_mail(_expired_record("urgent", urgency=Urgency.URGENT))
        await storage.save_mail(_expired_record("important", urgency=Urgency.IMPORTANT))
        await storage.save_mail(
            _expired_record("manual-ad", urgency=Urgency.AD, manual=Urgency.INFO)
        )
        await storage.save_mail(_expired_record("bare-info", urgency=Urgency.INFO))

        expired = await service.list_expired_mails()

        assert sorted(record.record_id for record in expired) == ["old-ad", "past-info"]
        assert await service.purge_expired_mails() == 2
        # exactly the expired records were handed to the trash move (the real
        # backend keeps them restorable; the error-prone part is the rule)
        assert sorted(storage.deleted) == ["old-ad", "past-info"]

    async def test_purge_is_a_noop_without_expired_mail(self) -> None:
        service = self.make_service()
        storage = cast(Any, service.storage)
        await storage.save_mail(_expired_record("fresh", urgency=Urgency.INFO))
        assert await service.purge_expired_mails() == 0
        assert len(await service.list_mails()) == 1


class TestUserProfile:
    """The recipient profile personalizes analysis and smart actions."""

    async def test_profile_round_trip_is_trimmed_and_capped(self) -> None:
        service = TestExpiredMail.make_service()
        assert await service.user_profile() == ""

        stored = await service.set_user_profile("  CS master's student, cares about labs  ")

        assert stored == "CS master's student, cares about labs"
        assert await service.user_profile() == stored

    async def test_profile_reaches_the_processor_context(self) -> None:
        seen: dict[str, str] = {}

        class Capturing:
            processor_id = "cap"

            async def process(self, mail: Any, context: Any) -> Any:
                seen["profile"] = context.user_profile
                return ProcessorResult()

        engine = PipelineEngine(
            [
                ProcessorBinding(
                    priority=10,
                    processor_id="cap",
                    plugin_id="test",
                    processor=Capturing(),
                    retries=0,
                    timeout_seconds=5.0,
                )
            ]
        )
        await engine.process(make_mail("m1", minute=1), "acct-1", user_profile="I ignore ads")

        assert seen["profile"] == "I ignore ads"

    async def test_smart_action_sends_the_profile_to_the_model(self) -> None:
        prompts: list[str] = []

        class RecordingRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                prompts.append(str(messages[-1]["content"]))
                return _reply('{"intent":"search"}')

        service = TestSmartAction.make_service(RecordingRouter())
        storage = cast(Any, service.storage)
        mail = make_mail("m1", minute=10)
        await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))
        await service.set_user_profile("I am a CS master's student")

        await service.smart_action("the lab notice")

        assert any("I am a CS master's student" in prompt for prompt in prompts)


class TestExpiredMailGuards:
    """The review found ways a sweep could delete the wrong mail; each is
    covered here so it cannot come back."""

    @staticmethod
    def _service() -> MailFlowService:
        return TestExpiredMail.make_service()

    @staticmethod
    def _rich(
        record_id: str,
        *,
        urgency: Urgency,
        age_hours: float = 72.0,
        due_in_hours: float | None = None,
        due_end_in_hours: float | None = None,
        summary_is_fallback: bool = False,
        failed_note: bool = False,
    ) -> MailRecord:
        record = _expired_record(
            record_id, urgency=urgency, age_hours=age_hours, due_in_hours=due_in_hours
        )
        assert record.analysis is not None
        if due_end_in_hours is not None:
            now = datetime.now(UTC)
            record.analysis.action_items[0].due_end = now + timedelta(hours=due_end_in_hours)
        record.analysis.summary_is_fallback = summary_is_fallback
        if failed_note:
            record.processor_notes.append(
                ProcessorNote(
                    processor_id="llm-importance",
                    plugin_id="mailflow-core",
                    status="failed",
                    message="failed: HTTP 500",
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                )
            )
        return record

    async def test_never_sweeps_a_mail_whose_analysis_failed(self) -> None:
        service = self._service()
        storage = cast(Any, service.storage)
        await storage.save_mail(self._rich("failed-ad", urgency=Urgency.AD, failed_note=True))
        await storage.save_mail(
            self._rich("fallback-ad", urgency=Urgency.AD, summary_is_fallback=True)
        )
        bare = _expired_record("no-analysis", urgency=Urgency.AD)
        await storage.save_mail(bare.model_copy(update={"analysis": None}))

        assert await service.list_expired_mails() == []

    async def test_never_sweeps_a_mail_with_a_live_schedule_entry(self) -> None:
        service = self._service()
        storage = cast(Any, service.storage)
        await storage.save_mail(self._rich("seminar-source", urgency=Urgency.INFO))
        # importing a seminar keeps the source mail id on the schedule entry
        await storage.save_custom_action(
            ActionItem(
                item_id="seminar-source-1",
                mail_id="seminar-source",
                summary="Attend the colloquium",
                action_type="seminar",
                due_at=datetime.now(UTC) + timedelta(days=3),
                origin=ActionOrigin.SEMINAR,
            )
        )

        assert await service.list_expired_mails() == []

    async def test_an_event_that_is_still_running_is_not_expired(self) -> None:
        service = self._service()
        storage = cast(Any, service.storage)
        await storage.save_mail(
            self._rich(
                "ongoing",
                urgency=Urgency.INFO,
                due_in_hours=-2,
                due_end_in_hours=20,
            )
        )

        assert await service.list_expired_mails() == []

    async def test_purge_rechecks_each_record_before_deleting_it(self) -> None:
        """A manual classification landing while the dialog is open wins."""
        service = self._service()
        storage = cast(Any, service.storage)
        await storage.save_mail(self._rich("sweepme", urgency=Urgency.AD))
        expired = await service.list_expired_mails()
        assert [record.record_id for record in expired] == ["sweepme"]

        # the user re-classifies the mail between the dialog and the confirm
        await storage.set_manual_urgency("sweepme", Urgency.URGENT)
        moved = await service.purge_expired_mails([record.record_id for record in expired])

        assert moved == 0
        assert storage.deleted == []
        stored = await service.get_mail("sweepme")
        assert stored is not None and stored.manual_urgency is Urgency.URGENT

    async def test_purge_only_touches_the_ids_it_was_given(self) -> None:
        service = self._service()
        storage = cast(Any, service.storage)
        await storage.save_mail(self._rich("first", urgency=Urgency.AD))
        await storage.save_mail(self._rich("second", urgency=Urgency.AD))

        assert await service.purge_expired_mails(["first"]) == 1

        assert storage.deleted == ["first"]
        remaining = {record.record_id for record in await service.list_mails()}
        assert remaining == {"second"}

    async def test_purge_refreshes_the_trash_window(self) -> None:
        """A mail already sitting in the trash must not expire right after."""
        service = self._service()
        storage = cast(Any, service.storage)
        await storage.save_mail(self._rich("stale-trash", urgency=Urgency.AD))

        assert await service.purge_expired_mails() == 1

        assert storage.refresh_deleted_at_calls == [True]


class TestFeedbackWindow:
    """One repeated rejection must not fill the whole prompt window."""

    async def test_repeated_notes_are_stored_once(self) -> None:
        service = TestExpiredMail.make_service()
        for _ in range(25):
            await service.record_feedback("m-reject", "这是营销广告，永远归为 ad")

        guidelines = (await service.feedback_guidelines()).splitlines()

        assert len(guidelines) == 1

    async def test_distinct_notes_survive_and_keep_the_newest(self) -> None:
        service = TestExpiredMail.make_service()
        await service.record_feedback("m1", "广告")
        await service.record_feedback("m2", "考试通知请保留")
        await service.record_feedback("m1", "广告")

        guidelines = (await service.feedback_guidelines()).splitlines()

        assert guidelines == ["m2: 考试通知请保留", "m1: 广告"]


class TestUrgencySynonyms:
    """A model answering in Chinese must not silently become info."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("important", Urgency.IMPORTANT),
            ("urgent", Urgency.URGENT),
            ("重要", Urgency.IMPORTANT),
            ("紧急", Urgency.URGENT),
            ("广告", Urgency.AD),
            ("推广", Urgency.AD),
            ("信息", Urgency.INFO),
            ("medium", Urgency.IMPORTANT),
            ("high", Urgency.URGENT),
        ],
    )
    def test_synonyms_resolve_to_the_contract_levels(self, value: str, expected: Urgency) -> None:
        assert parse_urgency(value) is expected

    def test_unknown_value_still_defaults_to_info(self) -> None:
        assert parse_urgency("??") is Urgency.INFO
