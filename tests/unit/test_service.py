"""Service workflows isolated from model, mail and persistent storage providers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from mailflow.config import LLMConfig, MailFlowConfig, ProcessorConfig
from mailflow.contracts import (
    LLMCompletion,
    LLMRouter,
    MailMessage,
    ProcessorResult,
    ReplyDraft,
)
from mailflow.domain import (
    ActionItem,
    ActionOrigin,
    MailAnalysis,
    MailRecord,
    ProcessorNote,
    ReplyState,
    SeminarCandidate,
    SeminarStatus,
    Urgency,
)
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine, ProcessorBinding
from mailflow.registry import ComponentRegistry
from mailflow.replies import ReplyError
from mailflow.service import MailFlowService

from mailflow_test_support.service import (
    ADDRESS,
    MemoryStorage,
    RecordingSource,
    make_record,
    model_completion,
)
from mailflow_test_support.service import service as service


def new_service(router: Any = None) -> MailFlowService:
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
        with pytest.raises(PermissionError):
            await service.confirm_reply(draft.draft_id, prepared.token or "")
        with pytest.raises(ReplyError):
            await service.edit_draft(draft.draft_id, "changed", "changed")
        with pytest.raises(ReplyError):
            await service.cancel_reply(draft.draft_id)
        assert len(source.sent) == 1

    async def test_token_lifecycle_rejects_unprepared_expired_and_invalidated_confirmation(
        self, service: MailFlowService
    ) -> None:
        draft = await service.create_reply("m1")
        with pytest.raises(PermissionError):
            await service.confirm_reply(draft.draft_id, "not-prepared")
        await service.prepare_reply(draft.draft_id)
        with pytest.raises(PermissionError):
            await service.confirm_reply(draft.draft_id, "wrong-token")
        stored: ReplyDraft = await service.storage.get_draft(draft.draft_id)  # type: ignore[attr-defined]
        stored.token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await service.storage.save_draft(stored)  # type: ignore[attr-defined]
        with pytest.raises(PermissionError):
            await service.confirm_reply(draft.draft_id, stored.token or "")
        prepared = await service.prepare_reply(draft.draft_id)
        await service.edit_draft(draft.draft_id, "Re: Hello", "changed")
        with pytest.raises(PermissionError):
            await service.confirm_reply(draft.draft_id, prepared.token or "")
        assert (await service.cancel_reply(draft.draft_id)).state is ReplyState.CANCELLED
        with pytest.raises(ValueError):
            await service.prepare_reply(draft.draft_id)
        assert not cast(RecordingSource, service.sources["acct-1"]).sent

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

    async def test_fetch_history_paginates_newest_first(self) -> None:
        mails = [make_mail(f"h{i}", minute=i) for i in range(5)]
        source = HistorySource(mails)
        service = self._service(source)
        assert service.history_accounts() == ["acct-1"]
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

    @staticmethod
    def model_completion(text: str) -> Any:
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
                    return TestSmartSearch.model_completion("ok")
                return TestSmartSearch.model_completion(
                    '```json\n[{"id": "m1", "relevance": 88}]\n```'
                )

        service = new_service(FencedRouter())
        storage = cast(Any, service.storage)
        for message_id, minute in (("m1", 20), ("m2", 10)):
            mail = make_mail(message_id, minute=minute)
            await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))
        stages: list[tuple[str, int, int, Any]] = []

        def _progress(stage: str, done: int, total: int, detail: Any) -> None:
            stages.append((stage, done, total, detail))

        result = await service.smart_search("the fee notice", progress=_progress)

        assert [record.mail.message_id for record in result.records] == ["m1"]
        assert result.is_complete
        assert result.total_mails == 2
        assert stages[0][:3] == ("warmup", 0, 1)
        assert stages[-1][:3] == ("match", 2, 2)

    async def test_prose_wrapped_legacy_candidate_refs_are_parsed(self) -> None:
        class ProseRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                if messages[-1]["content"].startswith("Reply with"):
                    return TestSmartSearch.model_completion("ok")
                return TestSmartSearch.model_completion('Matching candidate:\n["m1"]\nDone.')

        service = new_service(ProseRouter())
        storage = cast(Any, service.storage)
        for message_id, minute in (("m1", 10), ("m2", 20)):
            mail = make_mail(message_id, minute=minute)
            await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))

        result = await service.smart_search("anything about m2")

        # Candidate m1 is the newest mail in this batch, not a raw record id.
        assert [record.mail.message_id for record in result.records] == ["m2"]

    async def test_candidate_refs_are_case_insensitive_and_hide_record_ids(self) -> None:
        opaque_record_id = "MixedCase-Opaque-Record-ID@example.test"

        class AliasRouter:
            def __init__(self) -> None:
                self.listing = ""

            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                user = messages[-1]["content"]
                if user.startswith("Reply with"):
                    return TestSmartSearch.model_completion("ok")
                self.listing = user
                return TestSmartSearch.model_completion('[{"id": "M1", "relevance": 93}]')

        router = AliasRouter()
        service = new_service(router)
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
                    return TestSmartSearch.model_completion("ok")
                assert "线上研讨会通知" in user
                return TestSmartSearch.model_completion('[{"id": "m1", "relevance": 100}]')

        service = new_service(BilingualRouter())
        storage = cast(Any, service.storage)
        mail = make_mail("m1", minute=20).model_copy(update={"subject": "线上研讨会通知"})
        await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))
        other = make_mail("m2", minute=10)
        await storage.save_mail(MailRecord(record_id=other.normalized_message_id(), mail=other))

        result = await service.smart_search("我需要参加线上研讨会")

        assert [record.mail.message_id for record in result.records] == ["m1"]

    async def test_scored_matches_rank_across_batches(self) -> None:
        """A highly relevant older result must precede a newer weak match."""

        class RankingRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                user = messages[-1]["content"]
                if user.startswith("Reply with"):
                    return TestSmartSearch.model_completion("ok")
                if "Subject m15" in user:
                    return TestSmartSearch.model_completion('[{"id": "m1", "relevance": 60}]')
                return TestSmartSearch.model_completion('[{"id": "m1", "relevance": 95}]')

        service = new_service(RankingRouter())
        storage = cast(Any, service.storage)
        for index in range(16):
            mail = make_mail(f"m{index}", minute=index)
            await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))

        result = await service.smart_search("event details")

        assert [record.mail.message_id for record in result.records] == ["m0", "m15"]
        assert result.is_complete

    async def test_low_scored_candidates_are_not_returned(self) -> None:
        """Scores below the published floor are model non-matches, not results."""

        class PrecisionRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                if messages[-1]["content"].startswith("Reply with"):
                    return TestSmartSearch.model_completion("ok")
                return TestSmartSearch.model_completion(
                    '[{"id": "m1", "relevance": 30}, {"id": "m2", "relevance": 40}]'
                )

        service = new_service(PrecisionRouter())
        storage = cast(Any, service.storage)
        for message_id, minute in (("m1", 20), ("m2", 10)):
            mail = make_mail(message_id, minute=minute)
            await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))

        result = await service.smart_search("the student ID invitation")

        assert [record.mail.message_id for record in result.records] == ["m2"]

    async def test_malformed_batch_is_retried_then_reported_incomplete(self) -> None:
        class BadRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                if messages[-1]["content"].startswith("Reply with"):
                    return TestSmartSearch.model_completion("ok")
                return TestSmartSearch.model_completion("I could not find anything, sorry!")

        service = new_service(BadRouter())
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
                    return TestSmartSearch.model_completion("ok")
                if "Subject m15" in user:
                    return TestSmartSearch.model_completion('[{"id": "m1", "relevance": 90}]')
                raise TimeoutError("configured endpoint timed out")

        service = new_service(PartiallyFailingRouter())
        storage = cast(Any, service.storage)
        for index in range(16):
            mail = make_mail(f"m{index}", minute=index)
            await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))
        stages: list[tuple[str, int, int, Any]] = []

        def _progress(stage: str, done: int, total: int, detail: Any) -> None:
            stages.append((stage, done, total, detail))

        result = await service.smart_search("event details", progress=_progress)

        assert [record.mail.message_id for record in result.records] == ["m15"]
        assert result.failed_mails == 1
        assert result.failed_batches == 1
        assert not result.is_complete
        assert any(detail[0] == "smart_batch_failed" for *_rest, detail in stages)

    async def test_empty_mailbox_does_not_call_the_llm(self) -> None:
        class NoCallRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                raise AssertionError("an empty mailbox does not need an LLM")

        result = await new_service(NoCallRouter()).smart_search("anything")

        assert result.records == []
        assert result.total_mails == 0
        assert result.is_complete


class TestSeminarDiscovery:
    """Seminar discovery must remain review-only, timezone-safe and idempotent."""

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
                return model_completion(
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
        service = new_service(router)
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
        repeated = (await service.discover_seminars()).candidates[0]
        assert repeated.candidate_id == candidate.candidate_id
        assert repeated.title == "Reworded colloquium"

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

    async def test_malformed_batch_is_retried_and_reported_incomplete(self) -> None:
        class MalformedRouter:
            def __init__(self) -> None:
                self.calls = 0

            async def chat(self, messages: Any, **kwargs: Any) -> Any:
                self.calls += 1
                return model_completion('I found a seminar: {"id":"m1","title":"invented"} maybe?')

        router = MalformedRouter()
        service = new_service(router)
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
                return model_completion(
                    '[{"id":"m1","title":"Past seminar",'
                    '"starts_at":"2020-01-01T10:00:00+08:00",'
                    '"timezone":"Asia/Shanghai"}]'
                )

        service = new_service(PastSeminarRouter())
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
                return model_completion(
                    '[{"id":"m1","title":"Seminar",'
                    '"starts_at":"2030-02-01T10:00:00+08:00",'
                    '"timezone":"Asia/Shanghai"}]'
                )

        service = new_service(SeminarRouter())
        storage = cast(Any, service.storage)
        mail = make_mail("m1", minute=10)
        await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))
        candidate = (await service.discover_seminars()).candidates[0]

        assert await service.reject_seminar(candidate.candidate_id)
        result = await service.discover_seminars()

        assert result.candidates == []
        saved = await service.list_seminar_candidates(include_resolved=True)
        assert saved[0].status is SeminarStatus.REJECTED


@pytest.mark.parametrize("prefix", ["", "Here you go:\n```json\n"], ids=["raw", "fenced"])
async def test_truncated_discovery_keeps_complete_candidates(prefix: str) -> None:
    rows = [
        {
            "id": "m1",
            "title": "First",
            "starts_at": "2099-01-01T09:00:00Z",
            "meta": {"a": {"b": 1}},
        },
        {"id": "m2", "title": "Second", "starts_at": "2099-01-02T09:00:00Z"},
    ]

    class TruncatedRouter:
        async def chat(self, messages: Any, **kwargs: Any) -> Any:
            return model_completion(prefix + json.dumps(rows)[:-1] + ',{"id":"m3","title":"Cut')

    service = new_service(TruncatedRouter())
    for index in range(3):
        await service.storage.save_mail(
            MailRecord(
                record_id=f"record-{index}",
                mail=make_mail(f"mail-{index}", minute=3 - index),
            )
        )
    result = await service.discover_seminars()
    assert result.is_complete
    assert {(item.mail_id, item.title) for item in result.candidates} == {
        ("record-0", "First"),
        ("record-1", "Second"),
    }
    assert await service.list_actions() == []


async def test_smart_action_without_model_reports_unavailable() -> None:
    service = new_service(None)
    service.config.llms = []
    with pytest.raises(RuntimeError):
        await service.smart_action("find the exam mails")


class TestActionMutations:
    @staticmethod
    def make_service() -> MailFlowService:
        return new_service(None)

    async def test_add_and_edit_preserve_free_type_and_metadata(self) -> None:
        service = self.make_service()
        start = datetime(2099, 6, 1, 9, tzinfo=UTC)
        end = start + timedelta(hours=2)
        item = await service.add_action(
            "Submit the form",
            start,
            action_type="registration",
            due_end=end,
            notes="Bring the signed copy",
            location="Office 4",
            url="https://example.com/form",
        )

        assert item.action_type == "registration"
        assert item.due_at == start and item.due_end == end
        assert item.notes == "Bring the signed copy"
        assert item.location == "Office 4" and item.url == "https://example.com/form"

        updated = await service.edit_action(
            item.item_id,
            action_type="committee",
            due_end=end + timedelta(hours=1),
            notes="Updated notes",
            location="Office 5",
            url="https://example.com/updated",
        )

        assert updated.action_type == "committee"
        assert updated.due_end == end + timedelta(hours=1)
        assert updated.notes == "Updated notes"
        assert updated.location == "Office 5" and updated.url == "https://example.com/updated"
        cleared = await service.edit_action(item.item_id, clear_end=True)
        assert cleared.due_end is None

    async def test_action_times_must_be_aware_and_end_after_start(self) -> None:
        service = self.make_service()
        storage = cast(Any, service.storage)
        start = datetime(2099, 6, 1, 9, tzinfo=UTC)

        with pytest.raises(ValueError):
            await service.add_action("Naive", datetime(2099, 6, 1, 9))
        with pytest.raises(ValueError):
            await service.add_action("Zero window", start, due_end=start)
        item = await service.add_action("Window", start, due_end=start + timedelta(hours=2))
        with pytest.raises(ValueError):
            await service.edit_action(item.item_id, due_end=start)
        with pytest.raises(ValueError):
            await service.edit_action(item.item_id, due_at=start + timedelta(hours=2))

        assert await storage.list_custom_actions() == [item]

    async def test_add_and_edit_conflicts_leave_existing_actions_unchanged(self) -> None:
        service = self.make_service()
        storage = cast(Any, service.storage)
        start = datetime(2099, 6, 1, 9, tzinfo=UTC)
        end = start + timedelta(hours=2)
        existing = await service.add_action("Pay the fee", start, due_end=end, action_type="errand")

        with pytest.raises(ValueError) as add_error:
            await service.add_action(
                "  PAY   THE FEE  ", start + timedelta(minutes=30), action_type="meeting"
            )
        assert existing.summary in str(add_error.value)
        assert existing.time_range in str(add_error.value)

        other = await service.add_action("Submit the form", start, action_type="errand")
        with pytest.raises(ValueError) as edit_error:
            await service.edit_action(other.item_id, summary="pay the fee")
        assert existing.summary in str(edit_error.value)
        assert existing.time_range in str(edit_error.value)

        stored = {item.item_id: item for item in await storage.list_custom_actions()}
        assert stored[other.item_id].summary == "Submit the form"
        assert len(stored) == 2

    async def test_analysis_actions_cannot_be_edited(self) -> None:
        service = self.make_service()
        storage = cast(Any, service.storage)
        record = make_record()
        source_action = ActionItem(
            item_id="analysis-action",
            mail_id=record.mail.message_id,
            summary="Pay the fee",
            action_type="errand",
            due_at=datetime(2099, 6, 1, 9, tzinfo=UTC),
            origin=ActionOrigin.ANALYSIS,
        )
        await storage.save_mail(
            record.model_copy(
                update={
                    "analysis": MailAnalysis(
                        summary="notice",
                        urgency=Urgency.IMPORTANT,
                        action_items=[source_action],
                    )
                }
            )
        )

        with pytest.raises(ValueError, match="analysis-action"):
            await service.edit_action("analysis-action", summary="Changed")

        assert (await storage.list_mails())[0].action_items == [source_action]
        assert await storage.list_custom_actions() == []

    async def test_seminar_import_conflicts_do_not_write(self) -> None:
        service = self.make_service()
        storage = cast(Any, service.storage)
        start = datetime(2099, 6, 1, 9, tzinfo=UTC)
        candidate = SeminarCandidate(
            candidate_id="seminar-1",
            mail_id="m1",
            title="Research colloquium",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
        )
        await storage.set_preference("seminars.candidates", f"[{candidate.model_dump_json()}]")
        existing = ActionItem(
            item_id="custom-1",
            mail_id="",
            summary="[SEMINAR] Research colloquium",
            action_type="meeting",
            due_at=start,
            due_end=start + timedelta(hours=1),
            origin=ActionOrigin.CUSTOM,
        )
        await storage.save_custom_action(existing)

        with pytest.raises(ValueError) as error:
            await service.import_seminar(candidate.candidate_id)

        assert existing.summary in str(error.value)
        assert existing.time_range in str(error.value)
        assert await storage.list_custom_actions() == [existing]
        saved_candidate = await service.list_seminar_candidates(include_resolved=True)
        assert saved_candidate[0].status is SeminarStatus.PENDING

    async def test_reimport_of_title_drifted_same_event_returns_existing(self) -> None:
        """The same event announced by two mails yields two candidates whose
        titles drift; importing both must end with ONE schedule row."""
        service = self.make_service()
        storage = cast(Any, service.storage)
        start = datetime(2099, 6, 1, 9, tzinfo=UTC)
        end = start + timedelta(hours=1)
        first = SeminarCandidate(
            candidate_id="seminar-a",
            mail_id="m1",
            title="Graph Learning Seminar",
            starts_at=start,
            ends_at=end,
            location="Room 101",
        )
        second = SeminarCandidate(
            candidate_id="seminar-b",
            mail_id="m2",
            title="Seminars on graph learning",  # title drift, same event
            starts_at=start,
            ends_at=end,
            location="Room 101",
        )
        await storage.set_preference(
            "seminars.candidates",
            f"[{first.model_dump_json()}, {second.model_dump_json()}]",
        )

        imported_first = await service.import_seminar(first.candidate_id)
        imported_second = await service.import_seminar(second.candidate_id)

        assert imported_second == imported_first
        actions = await storage.list_custom_actions()
        assert len(actions) == 1
        # both candidates are marked imported so repeat scans stop offering them
        statuses = {
            candidate.status
            for candidate in await service.list_seminar_candidates(include_resolved=True)
        }
        assert statuses == {SeminarStatus.IMPORTED}


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


class TestClearAllRecords:
    """`make clean-records` moves everything to the trash and leaves it there."""

    async def test_clear_is_recoverable_repeatable_and_resets_derived_state(self) -> None:
        service = new_service()
        storage = cast(Any, service.storage)
        record = _expired_record("m1", urgency=Urgency.INFO, due_in_hours=5)
        await storage.save_mail(record)
        await storage.save_mail(_expired_record("already-deleted", urgency=Urgency.INFO))
        await service.delete_mail("already-deleted")
        await service.add_action("Submit the form", datetime.now(UTC) + timedelta(days=2))
        await storage.set_preference("actions.dismissed", '["stale|key|errand"]')
        await storage.set_preference("seminars.candidates", '[{"candidate_id":"stale"}]')

        moved, actions = await service.clear_all_records()

        assert moved == 1
        assert actions == 1
        assert await service.count_mails() == 0
        assert await service.list_actions_all() == []
        assert len(await service.list_trash()) == 2
        assert await storage.get_preference("actions.dismissed") == "[]"
        assert await storage.get_preference("seminars.candidates") == "[]"
        assert await service.clear_all_records() == (0, 0)
        assert len(await service.list_trash()) == 2
        restored = await service.process_mail(record.mail)
        assert restored is not None
        assert await service.storage.get_mail(restored.record_id) is not None
        assert await service.purge_trash_now() == 2
        assert await service.list_trash() == []
        assert await service.count_mails() == 1


class TestExpiredMail:
    """Clearing expired mail must never touch mail that still matters."""

    async def test_purges_old_ads_and_past_info_only(self) -> None:
        service = new_service()
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
        await storage.save_mail(
            TestExpiredMailGuards.rich_record("failed-ad", urgency=Urgency.AD, failed_note=True)
        )
        await storage.save_mail(
            TestExpiredMailGuards.rich_record(
                "fallback-ad", urgency=Urgency.AD, summary_is_fallback=True
            )
        )
        await storage.save_mail(
            _expired_record("no-analysis", urgency=Urgency.AD).model_copy(update={"analysis": None})
        )
        await storage.save_mail(
            TestExpiredMailGuards.rich_record(
                "ongoing", urgency=Urgency.INFO, due_in_hours=-2, due_end_in_hours=20
            )
        )
        await storage.save_mail(_expired_record("seminar-source", urgency=Urgency.INFO))
        await storage.save_custom_action(
            ActionItem(
                item_id="seminar",
                mail_id="seminar-source",
                summary="Colloquium",
                action_type="seminar",
                due_at=datetime.now(UTC) + timedelta(days=3),
                origin=ActionOrigin.SEMINAR,
            )
        )

        expired = await service.list_expired_mails()

        assert sorted(record.record_id for record in expired) == ["old-ad", "past-info"]
        assert await service.purge_expired_mails() == 2
        # exactly the expired records were handed to the trash move (the real
        # backend keeps them restorable; the error-prone part is the rule)
        assert sorted(storage.deleted) == ["old-ad", "past-info"]
        assert storage.refresh_deleted_at_calls == [True, True]
        assert await service.purge_expired_mails() == 0


class TestUserProfile:
    """The recipient profile personalizes analysis and smart actions."""

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
                return model_completion("")

        service = new_service(RecordingRouter())
        storage = cast(Any, service.storage)
        mail = make_mail("m1", minute=10)
        await storage.save_mail(MailRecord(record_id=mail.normalized_message_id(), mail=mail))
        assert await service.user_profile() == ""
        stored = await service.set_user_profile("  I am a CS master's student  ")
        assert await service.user_profile() == stored == "I am a CS master's student"

        await service.smart_action("the lab notice")

        assert any("I am a CS master's student" in prompt for prompt in prompts)


class TestExpiredMailGuards:
    """The review found ways a sweep could delete the wrong mail; each is
    covered here so it cannot come back."""

    @staticmethod
    def _service() -> MailFlowService:
        return new_service()

    @staticmethod
    def rich_record(
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

    async def test_purge_rechecks_each_record_before_deleting_it(self) -> None:
        """A manual classification landing while the dialog is open wins."""
        service = self._service()
        storage = cast(Any, service.storage)
        await storage.save_mail(self.rich_record("sweepme", urgency=Urgency.AD))
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
        await storage.save_mail(self.rich_record("first", urgency=Urgency.AD))
        await storage.save_mail(self.rich_record("second", urgency=Urgency.AD))

        assert await service.purge_expired_mails(["first"]) == 1

        assert storage.deleted == ["first"]
        remaining = {record.record_id for record in await service.list_mails()}
        assert remaining == {"second"}


class TestFeedbackWindow:
    """One repeated rejection must not fill the whole prompt window."""

    async def test_distinct_notes_survive_and_keep_the_newest(self) -> None:
        service = new_service()
        await service.record_feedback("m1", "广告")
        await service.record_feedback("m2", "考试通知请保留")
        for _ in range(25):
            await service.record_feedback("m1", "广告")

        guidelines = (await service.feedback_guidelines()).splitlines()

        assert guidelines == ["m2: 考试通知请保留", "m1: 广告"]


class TestLLMFallbackBinding:
    """A failing primary must route to the next named LLM."""

    @staticmethod
    def _service(config: MailFlowConfig, tmp_path: Path) -> MailFlowService:
        from mailflow.plugins import PluginManager

        manager = PluginManager(config)
        service = MailFlowService(
            config=config,
            registry=manager.build_registry(),
            plugin_manager=manager,
            storage=cast(Any, MemoryStorage()),
            sources={},
            router=cast(LLMRouter, None),
            pipeline=PipelineEngine([]),
            notifiers=[],
            notifier_configs=[],
            events=EventBus(),
            i18n=I18n(),
        )
        service.config_path = tmp_path / "cfg.toml"
        return service

    @staticmethod
    def _processor(config: MailFlowConfig) -> Any:
        return next(p for p in config.processors if p.provider == "llm-importance")

    async def test_pinning_a_model_to_the_top_moves_the_analysis(self, tmp_path: Path) -> None:
        """The chain order is the routing policy: pinning a new model must make
        analysis use it, or every mail waits out the old model's timeout first."""
        config = MailFlowConfig()
        config.llms = [LLMConfig(llm_id="old-slow"), LLMConfig(llm_id="new-fast")]
        config.processors = [
            ProcessorConfig(
                processor_id="llm-importance",
                provider="llm-importance",
                llm="old-slow",
                fallback_llms=["new-fast"],
            )
        ]
        service = self._service(config, tmp_path)

        # the user moves the fast model to the top of the chain
        await service.move_config_entry("llms", 1, -1)

        bound = self._processor(service.config)
        assert [llm.llm_id for llm in service.config.llms] == ["new-fast", "old-slow"]
        assert bound.llm == "new-fast"
        assert bound.fallback_llms == ["old-slow"]

    async def test_automatic_processor_timeout_covers_request_budget(self, tmp_path: Path) -> None:
        config = MailFlowConfig()
        config.llms = [LLMConfig(llm_id="slow", timeout_seconds=300.0)]
        service = self._service(config, tmp_path)

        await service.update_config_entry("llms", 0, {"model": "changed"})

        assert self._processor(service.config).timeout_seconds >= 300.0

    async def test_explicit_fallbacks_are_kept(self, tmp_path: Path) -> None:
        config = MailFlowConfig()
        config.llms = [LLMConfig(llm_id="primary"), LLMConfig(llm_id="backup")]
        config.processors = [
            ProcessorConfig(
                processor_id="llm-importance",
                provider="llm-importance",
                llm="primary",
                fallback_llms=["backup"],
            )
        ]
        service = self._service(config, tmp_path)

        await service.update_config_entry("llms", 0, {"model": "changed"})

        assert self._processor(service.config).fallback_llms == ["backup"]


class TestSeminarCandidateDedupe:
    """Discovery, review and import must agree on which events are distinct."""

    @pytest.mark.parametrize(
        ("overrides", "expected_mails"),
        [
            pytest.param(
                [{"id": "m1"}, {"id": "m1", "title": "  quantum   seminar ", "confidence": 95}],
                ["mail-one"],
                id="duplicate-within-mail",
            ),
            pytest.param(
                [{}, {"title": "  quantum   seminar ", "confidence": 95}],
                ["mail-two"],
                id="duplicate-across-mails",
            ),
            pytest.param(
                [{}, {"starts_at": "2099-11-09T06:00:00Z"}],
                ["mail-one", "mail-two"],
                id="different-dates",
            ),
            pytest.param(
                [{"starts_at": None}, {"starts_at": None, "confidence": 95}],
                ["mail-two"],
                id="untimed-title",
            ),
            pytest.param(
                [
                    {"location": "Room 101, Bldg B"},
                    {"title": "Reworded seminar", "location": "room 101, bldg b", "confidence": 95},
                ],
                ["mail-two"],
                id="same-venue-reworded-title",
            ),
            pytest.param(
                [
                    {"title": "Parallel Session A", "location": "Room 1"},
                    {"title": "Parallel Session B", "location": "Room 2"},
                ],
                ["mail-one", "mail-two"],
                id="parallel-events",
            ),
            pytest.param(
                [
                    {"url": "https://events.example.test/live"},
                    {
                        "title": "Reworded webinar",
                        "url": "https://events.example.test/live",
                        "confidence": 95,
                    },
                ],
                ["mail-two"],
                id="same-online-event",
            ),
        ],
    )
    async def test_discovery_lists_distinct_events_and_keeps_stable_ids(
        self, overrides: list[dict[str, Any]], expected_mails: list[str]
    ) -> None:
        payloads = [
            {
                "id": f"m{index + 1}",
                "title": "Quantum Seminar",
                "starts_at": "2099-11-02T06:00:00Z",
                "confidence": 70,
                **changes,
            }
            for index, changes in enumerate(overrides)
        ]

        class SeminarRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> LLMCompletion:
                return model_completion(json.dumps(payloads))

        service = new_service(SeminarRouter())
        for record_id, minute in [("mail-one", 10), ("mail-two", 9)]:
            await service.storage.save_mail(
                MailRecord(record_id=record_id, mail=make_mail(record_id, minute=minute))
            )
        first = await service.discover_seminars()
        assert first.is_complete and first.evaluated_mails == 2
        assert sorted(item.mail_id for item in first.candidates) == sorted(expected_mails)
        assert await service.list_seminar_candidates() == first.candidates
        assert await service.list_actions() == []
        repeated = await service.discover_seminars()
        assert {item.candidate_id for item in repeated.candidates} == {
            item.candidate_id for item in first.candidates
        }

    async def rediscover_imported_event(
        self,
    ) -> tuple[MailFlowService, SeminarCandidate, ActionItem]:
        payloads: list[dict[str, Any]] = [
            {
                "id": "m1",
                "title": "Already Imported",
                "starts_at": "2099-11-02T06:00:00Z",
                "confidence": 10,
            }
        ]

        class SeminarRouter:
            async def chat(self, messages: Any, **kwargs: Any) -> LLMCompletion:
                return model_completion(json.dumps(payloads))

        service = new_service(SeminarRouter())
        await service.storage.save_mail(
            MailRecord(record_id="mail-one", mail=make_mail("one", minute=10))
        )
        candidate = (await service.discover_seminars()).candidates[0]
        action = await service.import_seminar(candidate.candidate_id)
        await service.storage.save_mail(
            MailRecord(record_id="mail-two", mail=make_mail("two", minute=9))
        )
        payloads.append({**payloads[0], "id": "m2", "confidence": 99})

        await service.discover_seminars()
        return service, candidate, action

    async def test_imported_event_keeps_status_and_action_when_a_duplicate_arrives(self) -> None:
        service, candidate, action = await self.rediscover_imported_event()
        listed = await service.list_seminar_candidates(include_resolved=True)
        assert [(item.candidate_id, item.status) for item in listed] == [
            (candidate.candidate_id, SeminarStatus.IMPORTED)
        ]
        assert [item.item_id for item in await service.list_actions()] == [action.item_id]
        assert await service.list_seminar_candidates() == []

    @pytest.mark.parametrize("status", [SeminarStatus.IMPORTED, SeminarStatus.REJECTED])
    async def test_expiring_duplicate_does_not_hide_a_resolved_event(
        self, status: SeminarStatus
    ) -> None:
        service = new_service(None)
        resolved = SeminarCandidate(
            candidate_id="resolved",
            mail_id="one",
            title="Past event",
            starts_at=datetime.now(UTC) - timedelta(days=1),
            status=status,
        )
        duplicate = resolved.model_copy(
            update={"candidate_id": "duplicate", "mail_id": "two", "status": SeminarStatus.PENDING}
        )
        await service.storage.set_preference(
            "seminars.candidates",
            json.dumps([duplicate.model_dump(mode="json"), resolved.model_dump(mode="json")]),
        )
        assert await service.list_seminar_candidates() == []
        assert await service.list_seminar_candidates(include_resolved=True) == [resolved]


class TestMarketplaceService:
    @staticmethod
    def _service(
        storage: MemoryStorage, *, config: MailFlowConfig | None = None
    ) -> MailFlowService:
        from mailflow.plugins import PluginManager

        return MailFlowService(
            config=config or MailFlowConfig(),
            registry=ComponentRegistry(),
            plugin_manager=PluginManager(config),
            storage=cast(Any, storage),
            sources={},
            router=cast(LLMRouter, None),
            pipeline=PipelineEngine([]),
            notifiers=[],
            notifier_configs=[],
            events=EventBus(),
            i18n=I18n(),
        )

    async def test_remote_install_records_source_only_after_install(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mailflow.plugin_market import MarketPlugin, PluginMarket, Repository

        storage = MemoryStorage()
        service = self._service(storage)
        plugin = MarketPlugin(
            id="market-service-test", package="", source="https://example.invalid/plugin"
        )
        calls: list[str] = []

        class FakeMarket:
            def find(self, plugin_id: str) -> tuple[Repository, MarketPlugin] | None:
                return (
                    (Repository("test", "https://example.invalid"), plugin)
                    if plugin_id == plugin.id
                    else None
                )

            async def install(self, item: MarketPlugin) -> str:
                calls.append(item.id)
                return "installed"

        monkeypatch.setattr(
            PluginMarket, "is_installed", staticmethod(lambda *args, **kwargs: False)
        )
        service.market = cast(Any, FakeMarket())

        assert await service.plugin_install(plugin.id) == "installed"
        assert calls == [plugin.id]
        assert storage.preferences["plugin.source.market-service-test"] == plugin.source

    async def test_local_install_aggregates_partial_failures(self, tmp_path: Path) -> None:
        from mailflow.plugin_market import MarketPlugin

        storage = MemoryStorage()
        service = self._service(storage)
        root = tmp_path / "plugins"
        for plugin_id in ("alpha-local", "beta-local"):
            folder = root / plugin_id
            folder.mkdir(parents=True)
            (folder / "plugin.json").write_text(json.dumps({"id": plugin_id}), encoding="utf-8")

        class FakeMarket:
            async def install(self, item: MarketPlugin) -> str:
                if item.id == "beta-local":
                    raise RuntimeError("broken package")
                return "installed"

        (root / "alpha-local/pyproject.toml").write_text(
            '[project]\nname = "alpha-distribution"\n', encoding="utf-8"
        )
        service.market = cast(Any, FakeMarket())
        result = await service.plugin_install_local(root)
        assert "alpha-local" in result
        assert "beta-local" in result
        assert storage.preferences["plugin.source.alpha-local"] == str(root / "alpha-local")
        assert storage.preferences["plugin.package.alpha-local"] == "alpha-distribution"
        assert "plugin.source.beta-local" not in storage.preferences

    async def test_market_fetch_report_merges_failed_repository_cache(self) -> None:
        from mailflow.plugin_market import MarketFetchReport, MarketPlugin, Repository

        storage = MemoryStorage()
        service = self._service(storage)
        failed = Repository("offline", "https://example.invalid")
        cached = MarketPlugin(id="cached-plugin", name="Cached")
        await service.market_cache_save([(failed, cached)])

        class OfflineMarket:
            def list_plugins_report(self) -> MarketFetchReport:
                return MarketFetchReport(entries=[], failures=[(failed, "offline")])

        service.market = cast(Any, OfflineMarket())
        report = await service.market_fetch_report()

        assert report.used_cache is True
        assert [plugin.id for _repo, plugin in report.entries] == ["cached-plugin"]
        assert report.repositories == []

    async def test_uninstall_uses_cached_metadata_when_repository_is_offline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mailflow.plugin_market import MarketPlugin, PluginMarket, Repository

        storage = MemoryStorage()
        service = self._service(storage)
        plugin = MarketPlugin(
            id="cached-uninstall",
            package="cached-uninstall-package",
            source="https://example.invalid/plugin",
        )
        await service.market_cache_save(
            [(Repository("offline", "https://example.invalid"), plugin)]
        )
        calls: list[str] = []

        class OfflineMarket:
            def find(self, _plugin_id: str) -> None:
                raise OSError("offline")

            async def uninstall(self, item: MarketPlugin) -> str:
                calls.append(item.package)
                return "removed"

        monkeypatch.setattr(
            PluginMarket,
            "is_installed",
            staticmethod(lambda *_args, **_kwargs: True),
        )
        service.market = cast(Any, OfflineMarket())

        assert await service.plugin_uninstall(plugin.id) == "removed"
        assert calls == [plugin.package]

    async def test_bundled_plugin_cannot_be_uninstalled(self) -> None:
        from types import SimpleNamespace

        service = self._service(MemoryStorage())
        service.plugin_manager = cast(
            Any, SimpleNamespace(bundled_plugin_ids={"mailflow-bundled-test"})
        )
        with pytest.raises(ValueError, match="cannot be uninstalled"):
            await service.plugin_uninstall("mailflow-bundled-test")

    async def test_repository_edit_is_atomic_and_offline(self, tmp_path: Path) -> None:
        service = self._service(MemoryStorage())
        service.config_path = tmp_path / "config.toml"
        await service.plugin_repo_add("offline", "https://example.invalid/repo")
        names_before = [repo.name for repo in service.config.plugins.repositories]
        with pytest.raises(ValueError, match="already configured"):
            await service.plugin_repo_edit("offline", "mailflow-repo", "https://other.invalid")
        assert [repo.name for repo in service.config.plugins.repositories] == names_before
        await service.plugin_repo_edit("offline", "renamed", "file:///tmp/repo")
        assert any(
            repo.name == "renamed" and repo.url == "file:///tmp/repo"
            for repo in service.config.plugins.repositories
        )
