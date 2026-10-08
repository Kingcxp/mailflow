"""The tools the model may call: read tools answer, mutating tools only stage.

These tests drive ``ToolRegistry`` directly (no model involved) and assert the
one property that matters most: a mutating tool returns a
``PendingOperation`` and leaves storage untouched. Only a confirmed host call
ever writes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from mailflow.config import LLMConfig, MailFlowConfig
from mailflow.contracts import LLMCompletion, LLMRouter, MailMessage
from mailflow.domain import (
    ActionItem,
    MailAddress,
    MailAnalysis,
    MailRecord,
    SeminarCandidate,
    Urgency,
)
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine
from mailflow.registry import ComponentRegistry
from mailflow.service import MailFlowService
from mailflow.tools import ToolRegistry

ADDRESS = MailAddress(name="Sender", address="sender@example.com")


class MemoryStorage:
    """In-memory storage: the tests assert on what it was asked to change."""

    def __init__(self) -> None:
        self.mails: dict[str, MailRecord] = {}
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
        ordered = sorted(self.mails.values(), key=lambda r: r.mail.received_at, reverse=True)
        return ordered if limit is None else ordered[:limit]

    async def count_mails(self) -> int:
        return len(self.mails)

    async def delete_mail(self, record_id: str, *, refresh_deleted_at: bool = False) -> None:
        self.mails.pop(record_id, None)

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


def make_mail(
    message_id: str,
    *,
    subject: str,
    body: str = "body",
    minute: int = 0,
    received_at: datetime | None = None,
) -> MailMessage:
    when = received_at or datetime(2026, 1, 1, 9, minute, tzinfo=UTC)
    return MailMessage(
        message_id=message_id,
        account_id="acct-1",
        subject=subject,
        sender=ADDRESS,
        recipients=[],
        cc=[],
        date=when,
        received_at=when,
        body_text=body,
        body_html=f"<p>{body}</p>",
        provider="fake",
    )


class RankingRouter:
    """Ranks by subject: candidate refs are the batch's own ``m1``, ``m2``…

    ``scores`` maps a subject substring to its relevance; every candidate in
    the listing gets a score, so a test can assert both what matched and in
    which order.
    """

    def __init__(self, scores: dict[str, float], *, default: float = 5.0) -> None:
        self.scores = scores
        self.default = default
        self.prompts: list[str] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMCompletion:
        import json

        system = str(messages[0].get("content") or "")
        if not system.startswith("You order an already-filtered") and not system.startswith(
            "You are MailFlow's smart mail finder"
        ):
            return LLMCompletion(text="ok")  # warm-up
        self.prompts.append(system)
        listing = str(messages[-1].get("content") or "")
        scored: list[dict[str, Any]] = []
        for block in listing.split("candidate=")[1:]:
            ref = block.splitlines()[0].strip()
            subject = next(
                (
                    line[len("subject=") :]
                    for line in block.splitlines()
                    if line.startswith("subject=")
                ),
                "",
            )
            relevance = next(
                (
                    score
                    for needle, score in self.scores.items()
                    if needle.casefold() in subject.casefold()
                ),
                self.default,
            )
            scored.append({"id": ref, "relevance": relevance})
        scored.sort(key=lambda entry: entry["relevance"], reverse=True)
        return LLMCompletion(text=json.dumps(scored))


class _AlwaysCalls:
    """Router that asks for one tool call, then answers in prose."""

    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        self._name = name
        self._arguments = arguments
        self.calls = 0

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMCompletion:
        from mailflow.contracts import ToolCall

        self.calls += 1
        if not any(m.get("role") == "tool" for m in messages):
            return LLMCompletion(
                text="",
                tool_calls=[
                    ToolCall(call_id=f"c{self.calls}", name=self._name, arguments=self._arguments)
                ],
            )
        return LLMCompletion(text="Done.")


class DiscoveryRouter(RankingRouter):
    """Answers the seminar-extraction request with no proposals.

    ``discover_seminars`` asks for a JSON array of event objects; replying
    with an empty array is the well-formed "nothing found" answer, which is
    what most tests here want (the proposals come from stored state).
    """

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMCompletion:
        system = str(messages[0].get("content") or "")
        if system.startswith("You identify optional academic seminars"):
            return LLMCompletion(text="[]")
        return await super().chat(messages, **kwargs)


def make_service(router: Any = None) -> MailFlowService:
    config = MailFlowConfig()
    config.llms = [LLMConfig(llm_id="llm-1")]
    return MailFlowService(
        config=config,
        registry=ComponentRegistry(),
        plugin_manager=cast(Any, None),
        storage=cast(Any, MemoryStorage()),
        sources={},
        router=cast(LLMRouter, router or DiscoveryRouter({})),
        pipeline=PipelineEngine([]),
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        i18n=I18n(),
    )


async def seed(service: MailFlowService) -> None:
    storage = cast(Any, service.storage)
    await storage.save_mail(
        MailRecord(
            record_id="a",
            mail=make_mail("a", subject="Research seminar", body="Join the Seminar on Friday"),
            auto_urgency=Urgency.INFO,
            analysis=MailAnalysis(summary="seminar", urgency=Urgency.INFO),
        )
    )
    await storage.save_mail(
        MailRecord(
            record_id="b",
            mail=make_mail("b", subject="Guest lecture", body="A lecture without the keyword"),
            auto_urgency=Urgency.INFO,
        )
    )
    await storage.save_mail(
        MailRecord(
            record_id="c",
            mail=make_mail(
                "c",
                subject="Old seminar notice",
                body="This seminar happened last year",
                received_at=datetime(2024, 1, 1, tzinfo=UTC),
            ),
            auto_urgency=Urgency.INFO,
        )
    )


class TestFindMail:
    """A literal condition decides membership; it never gets filtered out."""

    async def test_contains_is_case_insensitive_and_substring(self) -> None:
        service = make_service()
        await seed(service)
        registry = ToolRegistry(service)

        text, staged = await registry.call("find_mail", {"contains": "SEMINAR"})

        assert staged is None
        assert text.startswith("2 mail(s) matched:")
        assert "ids: a, c" in text
        assert "Guest lecture" not in text

    async def test_contains_matches_the_body_not_only_the_subject(self) -> None:
        service = make_service()
        await seed(service)
        registry = ToolRegistry(service)

        text, _ = await registry.call("find_mail", {"contains": "without the keyword"})

        assert "ids: b" in text

    async def test_requires_a_condition_and_never_calls_the_model(self) -> None:
        router = RankingRouter({"Research seminar": 99})
        service = make_service(router)
        await seed(service)
        registry = ToolRegistry(service)

        text, staged = await registry.call("find_mail", {})

        assert text.startswith("error:")
        assert "contains" in text
        assert staged is None
        assert router.prompts == []  # no condition means no model call at all

    async def test_literal_matches_survive_a_low_semantic_score(self) -> None:
        """The keyword the user demanded outranks the model's opinion of it."""
        router = RankingRouter({"seminar": 3})
        service = make_service(router)
        await seed(service)
        registry = ToolRegistry(service)

        text, _ = await registry.call("find_mail", {"contains": "seminar", "query": "to attend"})

        assert "ids: a, c" in text
        # the narrowed prompt is the ordering-only one
        assert "return ALL of them" in router.prompts[0]

    async def test_semantic_only_search_lets_scores_decide(self) -> None:
        router = RankingRouter({"Research seminar": 91, "Guest lecture": 12})
        service = make_service(router)
        await seed(service)
        registry = ToolRegistry(service)

        text, _ = await registry.call("find_mail", {"query": "the seminar invitation"})

        # 12 is below the relevance floor, so the ranking decided membership
        assert "ids: a" in text
        assert "Guest lecture" not in text
        assert "Return [] only after evaluating" in router.prompts[0]

    async def test_reports_unavailable_batches_instead_of_claiming_success(self) -> None:
        class FailingRouter:
            async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMCompletion:
                raise RuntimeError("backend down")

        service = make_service(FailingRouter())
        await seed(service)
        registry = ToolRegistry(service)

        text, _ = await registry.call("find_mail", {"query": "anything"})

        assert "could not be checked" in text
        assert "retry" in text


class TestStagingTools:
    """Mutating tools describe what would happen and change nothing."""

    async def test_delete_mail_stages_and_leaves_storage_alone(self) -> None:
        service = make_service()
        await seed(service)
        registry = ToolRegistry(service)

        text, staged = await registry.call("delete_mail", {"record_ids": ["a"]})

        assert "staged" in text
        assert staged is not None
        assert staged.tool == "delete_mail"
        assert staged.record_ids == ["a"]
        assert await service.count_mails() == 3  # nothing moved

    async def test_delete_mail_rejects_an_unknown_id(self) -> None:
        service = make_service()
        await seed(service)
        registry = ToolRegistry(service)

        text, staged = await registry.call("delete_mail", {"record_ids": ["nope"]})

        assert text.startswith("error:")
        assert staged is None
        assert await service.count_mails() == 3

    async def test_delete_mail_accepts_a_comma_string(self) -> None:
        service = make_service()
        await seed(service)
        registry = ToolRegistry(service)

        _, staged = await registry.call("delete_mail", {"record_ids": "a, c"})

        assert staged is not None
        assert staged.record_ids == ["a", "c"]

    async def test_schedule_event_stages_a_future_event(self) -> None:
        service = make_service()
        registry = ToolRegistry(service)

        text, staged = await registry.call(
            "schedule_event",
            {
                "title": "Colloquium",
                "starts_at": "2099-10-15T14:00:00+08:00",
                "location": "Room 201",
            },
        )

        assert "staged" in text
        assert staged is not None
        assert staged.tool == "schedule_event"
        assert await service.storage.list_custom_actions() == []  # type: ignore[attr-defined]

    async def test_schedule_event_rejects_a_past_time(self) -> None:
        service = make_service()
        registry = ToolRegistry(service)

        text, staged = await registry.call(
            "schedule_event", {"title": "Yesterday", "starts_at": "1999-01-01T09:00:00+00:00"}
        )

        assert text.startswith("error:")
        assert "in the past" in text
        assert staged is None

    async def test_schedule_event_rejects_an_unparseable_time(self) -> None:
        service = make_service()
        registry = ToolRegistry(service)

        text, staged = await registry.call(
            "schedule_event", {"title": "Whenever", "starts_at": "next Tuesday"}
        )

        assert text.startswith("error:")
        assert staged is None

    async def test_add_and_edit_and_delete_action_stage_without_writing(self) -> None:
        service = make_service()
        registry = ToolRegistry(service)

        _, add = await registry.call(
            "add_action",
            {"summary": "Submit the form", "due_at": "2099-05-01T09:00:00+00:00"},
        )
        assert add is not None and add.tool == "add_action"
        assert await service.list_actions_all() == []

        item = await service.add_action("Existing", datetime(2099, 6, 1, 9, tzinfo=UTC))
        _, edit = await registry.call("edit_action", {"item_id": item.item_id, "summary": "New"})
        assert edit is not None
        assert edit.arguments["summary"] == "New"
        assert (await service.list_actions_all())[0].summary == "Existing"

        _, delete = await registry.call("delete_action", {"item_id": item.item_id})
        assert delete is not None
        assert delete.tool == "delete_action"
        assert len(await service.list_actions_all()) == 1

    async def test_edit_action_rejects_an_empty_change_set(self) -> None:
        service = make_service()
        registry = ToolRegistry(service)
        item = await service.add_action("Existing", datetime(2099, 6, 1, 9, tzinfo=UTC))

        text, staged = await registry.call("edit_action", {"item_id": item.item_id})

        assert text.startswith("error:")
        assert "at least one field" in text
        assert staged is None

    async def test_edit_action_rejects_an_unknown_id(self) -> None:
        service = make_service()
        registry = ToolRegistry(service)

        text, staged = await registry.call("edit_action", {"item_id": "missing", "summary": "x"})

        assert text.startswith("error:")
        assert staged is None


class TestActionTools:
    async def test_list_actions_reports_ids_and_times(self) -> None:
        service = make_service()
        registry = ToolRegistry(service)
        await service.add_action("Submit the form", datetime(2099, 5, 1, 9, tzinfo=UTC))

        text, staged = await registry.call("list_actions", {})

        assert staged is None
        assert "Submit the form" in text
        assert "2099-05-01T09:00:00+00:00" in text

    async def test_list_actions_says_when_it_is_empty(self) -> None:
        service = make_service()
        registry = ToolRegistry(service)

        text, _ = await registry.call("list_actions", {})

        assert "empty" in text


class TestMiscTools:
    async def test_unknown_tool_is_reported_and_does_not_raise(self) -> None:
        service = make_service()
        registry = ToolRegistry(service)

        text, staged = await registry.call("does_not_exist", {})

        assert text == "unknown tool: does_not_exist"
        assert staged is None

    async def test_a_tool_bug_is_reported_to_the_model_not_raised(self) -> None:
        """``run_agent`` catches a tool bug and answers with an error text."""
        from mailflow.tools import run_agent

        service = make_service(_AlwaysCalls("delete_mail", {"record_ids": ["a"]}))
        await seed(service)

        async def boom(record_id: str) -> MailRecord | None:
            raise RuntimeError("storage exploded")

        service.get_mail = boom  # type: ignore[method-assign]
        result = await run_agent(service, cast(Any, service.router), "delete mail a")

        assert result.pending == []
        assert await service.count_mails() == 3

    async def test_check_seminars_reports_the_candidate_ids(self) -> None:
        service = make_service()
        await seed(service)
        registry = ToolRegistry(service)
        storage = cast(Any, service.storage)
        candidate = SeminarCandidate(
            candidate_id="seminar-1",
            mail_id="a",
            title="Research seminar",
            starts_at=datetime(2099, 10, 15, 6, tzinfo=UTC),
            timezone="UTC",
            confidence=90,
        )
        import json

        await storage.set_preference(
            "seminars.candidates", json.dumps([candidate.model_dump(mode="json")])
        )

        text, staged = await registry.call("check_seminars", {})

        assert staged is None
        assert "candidate_id=seminar-1" in text
        assert "2099-10-15" in text

    async def test_specs_describe_every_tool_with_a_json_schema(self) -> None:
        service = make_service()
        specs = ToolRegistry(service).specs()

        names = {spec["function"]["name"] for spec in specs}
        assert names == {
            "find_mail",
            "delete_mail",
            "schedule_event",
            "list_actions",
            "add_action",
            "edit_action",
            "delete_action",
            "check_seminars",
            "schedule_seminar",
            "schedule_seminars",
        }
        for spec in specs:
            assert spec["type"] == "function"
            assert spec["function"]["description"]
            assert spec["function"]["parameters"]["type"] == "object"


@pytest.mark.parametrize("future_hours", [1.0])
async def test_staged_event_survives_a_confirmed_apply(
    future_hours: float,
) -> None:
    """The staged arguments are exactly what the host can apply."""
    service = make_service()
    registry = ToolRegistry(service)
    starts = datetime.now(UTC) + timedelta(hours=future_hours)

    _, staged = await registry.call(
        "schedule_event", {"title": "Soon", "starts_at": starts.isoformat()}
    )

    assert staged is not None
    applied = await service.add_action(
        str(staged.arguments["title"]),
        datetime.fromisoformat(str(staged.arguments["starts_at"])),
    )
    assert (await service.list_actions_all())[0].item_id == applied.item_id


class TestCheckSeminarsScope:
    """A scan of some mails must not report candidates from other mails."""

    @staticmethod
    async def _seed_candidates(service: MailFlowService) -> None:
        import json

        storage = cast(Any, service.storage)
        inside = SeminarCandidate(
            candidate_id="seminar-inside",
            mail_id="a",
            title="Inside the requested set",
            starts_at=datetime(2099, 10, 15, 6, tzinfo=UTC),
            timezone="UTC",
        )
        outside = SeminarCandidate(
            candidate_id="seminar-outside",
            mail_id="b",
            title="From a mail the caller never asked about",
            starts_at=datetime(2099, 11, 15, 6, tzinfo=UTC),
            timezone="UTC",
        )
        await storage.set_preference(
            "seminars.candidates",
            json.dumps([inside.model_dump(mode="json"), outside.model_dump(mode="json")]),
        )

    async def test_results_stay_inside_the_requested_mail_set(self) -> None:
        service = make_service()
        await seed(service)
        await self._seed_candidates(service)
        registry = ToolRegistry(service)

        text, _ = await registry.call("check_seminars", {"record_ids": ["a"]})

        assert "seminar-inside" in text
        assert "seminar-outside" not in text
        assert "1 mail(s) you asked about" in text

    async def test_a_whole_mailbox_scan_is_not_scoped(self) -> None:
        service = make_service()
        await seed(service)
        await self._seed_candidates(service)
        registry = ToolRegistry(service)

        text, _ = await registry.call("check_seminars", {})

        assert "seminar-inside" in text
        assert "seminar-outside" in text

    async def test_a_candidate_without_a_time_is_flagged_as_untimed(self) -> None:
        import json

        service = make_service()
        await seed(service)
        storage = cast(Any, service.storage)
        untimed = SeminarCandidate(
            candidate_id="seminar-untimed", mail_id="a", title="Talks about talks"
        )
        await storage.set_preference(
            "seminars.candidates", json.dumps([untimed.model_dump(mode="json")])
        )
        registry = ToolRegistry(service)

        text, _ = await registry.call("check_seminars", {})

        assert "no start time" in text
        assert "unknown (no time in the mail)" in text

    async def test_an_incomplete_scan_tells_the_model_to_retry(self) -> None:
        class FailingRouter:
            async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMCompletion:
                raise TimeoutError("endpoint timed out")

        service = make_service(FailingRouter())
        await seed(service)
        registry = ToolRegistry(service)

        text, staged = await registry.call("check_seminars", {})

        assert text.startswith("error:")
        assert "retry" in text
        assert staged is None


class TestBulkSeminarStaging:
    """Many candidates must be stageable in one call, not one step each.

    The loop has a 12-step budget, so staging one candidate per call put a hard
    ceiling on how many seminars could ever reach the schedule — a scan of a
    few hundred mails yields dozens.
    """

    async def test_many_candidates_stage_in_a_single_call(self) -> None:
        from mailflow.domain import SeminarCandidate

        service = make_service()
        await seed(service)
        storage = cast(Any, service.storage)
        import json

        soon = datetime(2099, 5, 1, 9, tzinfo=UTC)
        candidates = [
            SeminarCandidate(
                candidate_id=f"sem-{index}",
                mail_id="a",
                title=f"Seminar {index}",
                starts_at=soon,
                timezone="UTC",
            )
            for index in range(25)
        ]
        await storage.set_preference(
            "seminars.candidates",
            json.dumps([candidate.model_dump(mode="json") for candidate in candidates]),
        )
        registry = ToolRegistry(service)

        text, staged = await registry.call(
            "schedule_seminars", {"candidate_ids": [f"sem-{i}" for i in range(25)]}
        )

        assert staged is not None
        assert len(staged.arguments["candidate_ids"]) == 25
        assert "25 seminar(s)" in text
        assert await service.storage.list_custom_actions() == []  # type: ignore[attr-defined]

    async def test_unknown_and_untimed_ids_are_skipped_not_fatal(self) -> None:
        from mailflow.domain import SeminarCandidate

        service = make_service()
        await seed(service)
        storage = cast(Any, service.storage)
        import json

        good = SeminarCandidate(
            candidate_id="good",
            mail_id="a",
            title="Timed",
            starts_at=datetime(2099, 5, 1, tzinfo=UTC),
        )
        untimed = SeminarCandidate(candidate_id="untimed", mail_id="a", title="No time")
        await storage.set_preference(
            "seminars.candidates",
            json.dumps([c.model_dump(mode="json") for c in (good, untimed)]),
        )
        registry = ToolRegistry(service)

        text, staged = await registry.call(
            "schedule_seminars", {"candidate_ids": ["good", "untimed", "ghost"]}
        )

        assert staged is not None
        assert staged.arguments["candidate_ids"] == ["good"]
        assert "2 skipped" in text

    async def test_a_fully_unusable_set_is_rejected(self) -> None:
        service = make_service()
        await seed(service)
        registry = ToolRegistry(service)

        text, staged = await registry.call("schedule_seminars", {"candidate_ids": ["ghost"]})

        assert text.startswith("error:")
        assert staged is None


class TestAttachmentNameSearch:
    """An announcement often names its topic only in the attached poster's file.

    The body carries the logistics while the subject line of the event lives in
    the image's file name ("...-Seminar-15Oct.jpg"), so a literal search that
    only folded subject/body/html missed mails that plainly concern the word
    the user typed.
    """

    async def test_a_filename_hit_is_found(self) -> None:
        from mailflow.domain import Attachment

        service = make_service()
        storage = cast(Any, service.storage)
        mail = make_mail("p1", subject="Invitation", body="See the poster attached.")
        mail.attachments.append(
            Attachment(filename="PAIR-Seminar-15Oct2026.jpg", content_type="image/jpeg", size=1234)
        )
        await storage.save_mail(MailRecord(record_id="p1", mail=mail))
        registry = ToolRegistry(service)

        text, _ = await registry.call("find_mail", {"contains": "seminar"})

        assert "ids: p1" in text

    async def test_a_filename_hit_alone_does_not_leak_other_mails(self) -> None:
        from mailflow.domain import Attachment

        service = make_service()
        storage = cast(Any, service.storage)
        with_poster = make_mail("p1", subject="Invitation", body="See attached.")
        with_poster.attachments.append(
            Attachment(filename="Research-Seminar.jpg", content_type="image/jpeg", size=99)
        )
        without = make_mail("p2", subject="Newsletter", body="Nothing of the sort here.")
        await storage.save_mail(MailRecord(record_id="p1", mail=with_poster))
        await storage.save_mail(MailRecord(record_id="p2", mail=without))
        registry = ToolRegistry(service)

        text, _ = await registry.call("find_mail", {"contains": "seminar"})

        assert "ids: p1" in text
        assert "p2" not in text


class TestImageTextSearch:
    """A poster-only mail must be findable by the word printed on the poster."""

    async def test_a_gap_split_word_on_a_poster_is_found(self) -> None:
        service = make_service()
        storage = cast(Any, service.storage)
        mail = make_mail("poster-1", subject="Invitation", body="See the poster attached.")
        mail = mail.model_copy(update={"image_text": "PAIR Research Se minar\n15 October 2026"})
        await storage.save_mail(MailRecord(record_id="poster-1", mail=mail))
        registry = ToolRegistry(service)

        text, _ = await registry.call("find_mail", {"contains": "seminar"})

        assert "ids: poster-1" in text, text

    async def test_plain_text_search_still_works_alongside(self) -> None:
        """The gap-closed form must not break ordinary matching."""
        service = make_service()
        await seed(service)
        registry = ToolRegistry(service)

        text, _ = await registry.call("find_mail", {"contains": "research seminar"})

        assert "ids: a" in text

    async def test_a_mail_without_image_text_is_unaffected(self) -> None:
        service = make_service()
        await seed(service)
        registry = ToolRegistry(service)

        text, _ = await registry.call("find_mail", {"contains": "seminar"})

        assert "ids: a, c" in text
        assert "poster" not in text
