"""The tools the model may call: read tools answer, mutating tools only stage.

These tests drive ``ToolRegistry`` directly (no model involved) and assert the
one property that matters most: a mutating tool returns a
``PendingOperation`` and leaves storage untouched. Only a confirmed host call
ever writes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from mailflow.config import LLMConfig, MailFlowConfig
from mailflow.contracts import LLMCompletion, LLMRouter, MailMessage
from mailflow.domain import (
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

from mailflow_test_support.service import OrderedMemoryStorage as MemoryStorage

ADDRESS = MailAddress(name="Sender", address="sender@example.com")


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
        if system.startswith("You identify optional academic events"):
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

        text, _ = await registry.call("find_mail", {"contains": "SEMINAR", "query": "to attend"})

        assert "ids: a, c" in text
        # the narrowed prompt is the ordering-only one

    async def test_semantic_only_search_lets_scores_decide(self) -> None:
        router = RankingRouter({"Research seminar": 91, "Guest lecture": 12})
        service = make_service(router)
        await seed(service)
        registry = ToolRegistry(service)

        text, _ = await registry.call("find_mail", {"query": "the seminar invitation"})

        # 12 is below the relevance floor, so the ranking decided membership
        assert "ids: a" in text
        assert "Guest lecture" not in text

    async def test_reports_unavailable_batches_instead_of_claiming_success(self) -> None:
        class FailingRouter:
            async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMCompletion:
                if kwargs.get("tools"):
                    from mailflow.contracts import ToolCall

                    if messages[-1]["role"] == "tool":
                        assert "search_id:" not in messages[-1]["content"]
                        return LLMCompletion(text="The search is incomplete.")
                    return LLMCompletion(
                        text="",
                        tool_calls=[
                            ToolCall(
                                call_id="find", name="find_mail", arguments={"query": "anything"}
                            )
                        ],
                    )
                raise RuntimeError("backend down")

        service = make_service(FailingRouter())
        await seed(service)
        progress: list[Any] = []

        def report(stage: str, done: int, total: int, detail: Any) -> None:
            progress.append((stage, done, total, detail))

        result = await service.smart_action("find anything", progress=report)

        assert not result.is_complete
        assert result.failed_mails == 3
        assert result.failed_batches == 1
        assert result.failure_reasons
        assert any(detail[0] == "smart_batch_failed" for *_rest, detail in progress)

    async def test_search_token_stages_every_match_beyond_preview_limit(self) -> None:
        service = make_service()
        storage = cast(Any, service.storage)
        for index in range(45):
            mail = make_mail(f"mail-{index}", subject=f"Seminar {index}", minute=index)
            await storage.save_mail(MailRecord(record_id=f"mail-{index}", mail=mail))
        registry = ToolRegistry(service, allow_mail_delete=True)

        text, staged = await registry.call("find_mail", {"contains": "seminar", "limit": 2})

        assert staged is None
        assert "matched: 45 mail(s); showing 2" in text
        assert text.count("- id=") == 2
        token_line = next(line for line in text.splitlines() if line.startswith("search_id: "))
        search_id = token_line.removeprefix("search_id: ")
        _, pending = await registry.call("delete_mail", {"search_id": search_id})

        expected_ids = [f"mail-{index}" for index in reversed(range(45))]
        assert pending is not None
        assert pending.record_ids == expected_ids
        assert pending.arguments["record_ids"] == expected_ids
        assert await service.count_mails() == 45
        repeated, no_pending = await registry.call("delete_mail", {"search_id": search_id})
        assert repeated.startswith("error:")
        assert no_pending is None
        assert await service.count_mails() == 45


class TestStagingTools:
    """Mutating tools describe what would happen and change nothing."""

    async def test_delete_mail_stages_only_with_explicit_authorization(self) -> None:
        service = make_service()
        await seed(service)
        denied = ToolRegistry(service)

        text, staged = await denied.call("delete_mail", {"record_ids": ["a"]})

        assert text == "error: mail deletion requires an explicit delete request"
        assert staged is None
        assert await service.count_mails() == 3

        allowed = ToolRegistry(service, allow_mail_delete=True)
        text, staged = await allowed.call("delete_mail", {"record_ids": ["a"]})
        assert "staged" in text
        assert staged is not None
        assert staged.tool == "delete_mail"
        assert staged.record_ids == ["a"]
        assert await service.count_mails() == 3  # only confirmation may write

    async def test_delete_mail_rejects_an_unknown_id(self) -> None:
        service = make_service()
        await seed(service)
        registry = ToolRegistry(service, allow_mail_delete=True)

        text, staged = await registry.call("delete_mail", {"record_ids": ["nope"]})

        assert text.startswith("error:")
        assert staged is None
        assert await service.count_mails() == 3

    async def test_schedule_event_stages_a_future_event(self) -> None:
        service = make_service()
        registry = ToolRegistry(service)

        text, staged = await registry.call(
            "schedule_event",
            {
                "title": "Colloquium",
                "starts_at": "2099-10-15T14:00:00+08:00",
                "due_end": "2099-10-15T16:00:00+08:00",
                "action_type": "registration",
                "location": "Room 201",
                "url": "https://example.com/event",
                "notes": "Bring the signed form",
            },
        )

        assert "staged" in text
        assert staged is not None
        assert staged.tool == "schedule_event"
        assert staged.arguments["due_end"] == "2099-10-15T08:00:00+00:00"
        assert staged.arguments["action_type"] == "registration"
        assert staged.arguments["location"] == "Room 201"
        assert staged.arguments["url"] == "https://example.com/event"
        assert staged.arguments["notes"] == "Bring the signed form"
        assert await service.storage.list_custom_actions() == []  # type: ignore[attr-defined]

        service.config.general.timezone = "Asia/Shanghai"
        _, local = await registry.call(
            "schedule_event", {"title": "Local event", "starts_at": "2099-10-15T14:00:00"}
        )
        assert local is not None
        assert local.arguments["starts_at"] == "2099-10-15T06:00:00+00:00"

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
            {
                "summary": "Submit the form",
                "due_at": "2099-05-01T09:00:00+00:00",
                "due_end": "2099-05-01T10:00:00+00:00",
                "action_type": "registration",
                "notes": "Sign page 2",
                "location": "Office 4",
                "url": "https://example.com/form",
            },
        )
        assert add is not None and add.tool == "add_action"
        assert add.arguments["due_end"] == "2099-05-01T10:00:00+00:00"
        assert add.arguments["action_type"] == "registration"
        assert add.arguments["notes"] == "Sign page 2"
        assert add.arguments["location"] == "Office 4"
        assert add.arguments["url"] == "https://example.com/form"
        assert await service.list_actions_all() == []

        item = await service.add_action("Existing", datetime(2099, 6, 1, 9, tzinfo=UTC))
        _, edit = await registry.call(
            "edit_action",
            {
                "item_id": item.item_id,
                "summary": "New",
                "action_type": "committee",
                "due_end": "2099-06-01T11:00:00+00:00",
                "location": "Room 7",
                "url": "https://example.com/updated",
            },
        )
        assert edit is not None
        assert edit.arguments["action_type"] == "committee"
        assert edit.arguments["due_end"] == "2099-06-01T11:00:00+00:00"
        assert edit.arguments["location"] == "Room 7"
        assert edit.arguments["url"] == "https://example.com/updated"
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
        empty, _ = await registry.call("list_actions", {})
        assert "empty" in empty
        await service.add_action("Submit the form", datetime(2099, 5, 1, 9, tzinfo=UTC))

        text, staged = await registry.call("list_actions", {})

        assert staged is None
        assert "Submit the form" in text
        assert "2099-05-01T09:00:00+00:00" in text


class TestMiscTools:
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

    async def test_check_seminars_scans_every_search_id_match(self) -> None:
        """find_mail's 40-id preview is a display cap: passing its search_id
        to check_seminars must scan the COMPLETE matched set, so a candidate
        hiding in match #45 is still reported."""
        service = make_service()
        await seed(service)
        storage = cast(Any, service.storage)
        registry = ToolRegistry(service)
        # 45 mails that contain the keyword; the candidate lives in #45, far
        # beyond the preview cap
        for index in range(45):
            await storage.save_mail(
                MailRecord(
                    record_id=f"bulk-{index}",
                    mail=make_mail(
                        f"bulk-{index}",
                        subject=f"Seminar notice {index}",
                        body="a seminar announcement",
                    ),
                    auto_urgency=Urgency.INFO,
                )
            )
        candidate = SeminarCandidate(
            candidate_id="seminar-45",
            mail_id="bulk-44",
            title="Deep in the match set",
            starts_at=datetime(2099, 10, 15, 6, tzinfo=UTC),
            timezone="UTC",
            confidence=90,
        )
        import json

        await storage.set_preference(
            "seminars.candidates", json.dumps([candidate.model_dump(mode="json")])
        )

        find_text, _ = await registry.call("find_mail", {"contains": "seminar"})
        search_id = next(
            line.split("search_id: ", 1)[1].strip()
            for line in find_text.splitlines()
            if line.startswith("search_id: ")
        )
        assert "bulk-44" not in find_text  # beyond the preview

        text, staged = await registry.call("check_seminars", {"search_id": search_id})

        assert "candidate_id=seminar-45" in text
        assert "you asked about" in text
        assert staged is None
        # one-use token
        second, _ = await registry.call("check_seminars", {"search_id": search_id})
        assert second.startswith("error:")

    async def test_check_seminars_rejects_both_selectors(self) -> None:
        service = make_service()
        await seed(service)
        registry = ToolRegistry(service)

        text, _ = await registry.call(
            "check_seminars", {"record_ids": ["a"], "search_id": "whatever"}
        )

        assert "not both" in text


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
            json.dumps(
                [
                    inside.model_dump(mode="json"),
                    outside.model_dump(mode="json"),
                    SeminarCandidate(
                        candidate_id="untimed", mail_id="a", title="No time"
                    ).model_dump(mode="json"),
                ]
            ),
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
        assert "untimed" in text and "no start time" in text
        whole_mailbox, _ = await registry.call("check_seminars", {})
        assert "seminar-inside" in whole_mailbox and "seminar-outside" in whole_mailbox

    async def test_an_incomplete_scan_reports_partial_results_and_the_gap(self) -> None:
        class FailingRouter:
            async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMCompletion:
                raise TimeoutError("endpoint timed out")

        service = make_service(FailingRouter())
        await seed(service)
        await self._seed_candidates(service)
        registry = ToolRegistry(service)

        text, staged = await registry.call("check_seminars", {})

        # A failed batch must not void the scan: the model gets the real
        # candidates plus which mails it could not read, so it can finish
        # the scan incrementally instead of restarting (or giving up and
        # adding entries one step at a time).
        assert text.startswith("partial:")
        assert "could not be read" in text
        assert "record_ids" in text
        assert "seminar-inside" in text and "seminar-outside" in text
        assert "failed_record_ids:" in text
        failed_ids = text.split("failed_record_ids: ", 1)[1].splitlines()[0]
        assert set(failed_ids.split(", ")) == {"a", "b", "c"}
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
        text, staged = await registry.call("schedule_seminars", {"candidate_ids": ["ghost"]})
        assert text.startswith("error:") and staged is None
