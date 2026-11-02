"""Unit tests for the LLM importance processor and JSON extraction."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from mailflow.config import ProcessorConfig
from mailflow.contracts import LLMCompletion, ProcessingContext
from mailflow.domain import Urgency
from mailflow.processors import (
    LLMImportanceProcessor,
    extract_json,
)
from mailflow_testkit.fakes import make_mail

CONTEXT = ProcessingContext(
    account_id="acct-1", timezone="UTC", now=datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
)

CRITICAL_EXAM_JSON = """{
  "summary": "Final calculus exam on June 10 at 09:00",
  "urgency": "urgent",
  "reason": "attendance is mandatory and requires preparation",
  "reply_required": true,
  "suggested_reply": "I will attend the exam.",
  "action_items": [
    {
      "summary": "Attend the final calculus exam",
      "must_do": true,
      "action_type": "exam",
      "due_at": "2026-06-10T09:00:00+08:00",
      "due_end": "2026-06-10T11:00:00+08:00",
      "notes": "Bring student ID and calculator"
    }
  ],
  "notes": "The exam hall is Building C, Room 302"
}"""


class StubRouter:
    """Returns a canned completion for every request."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.last_messages: list[dict[str, str]] = []

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMCompletion:
        self.last_messages = messages
        return LLMCompletion(
            text=self.text, model="m1", llm_id="primary", backend="openai-compatible"
        )


def make_processor(router: StubRouter) -> LLMImportanceProcessor:
    config = ProcessorConfig(
        processor_id="p1",
        provider="llm-importance",
        llm="primary",
        fallback_llms=["backup"],
    )
    return LLMImportanceProcessor(config, router)  # type: ignore[arg-type]


class TestExtractJson:
    def test_prose_wrapped_json_brace_slice(self) -> None:
        text = 'Sure, the answer is {"urgency": "urgent"} done.'
        assert extract_json(text) == {"urgency": "urgent"}

    def test_unparseable_raises(self) -> None:
        with pytest.raises((json.JSONDecodeError, ValueError)):
            extract_json("no json here")


class TestLLMImportanceProcessor:
    async def test_critical_exam_with_student_id(self) -> None:
        payload = CRITICAL_EXAM_JSON.replace('"urgent"', '"Critical"')
        router = StubRouter(
            '<think>{"summary": "Private reasoning"}</think>\n'
            f"Here is the answer:\n```json\n{payload}\n```\nDone."
        )
        processor = make_processor(router)
        mail = make_mail(
            subject="Final calculus exam", body_text="Bring your student ID to the exam hall."
        )
        result = await processor.process(mail, CONTEXT)

        assert result.analysis is not None
        analysis = result.analysis
        assert analysis.urgency is Urgency.URGENT
        assert analysis.summary == "Final calculus exam on June 10 at 09:00"
        assert analysis.reply_required is True
        assert analysis.suggested_reply == "I will attend the exam."
        assert result.llm_used == "primary"
        assert result.llm_backend == "openai-compatible"

        assert len(analysis.action_items) == 1
        item = analysis.action_items[0]
        assert item.mail_id == mail.message_id
        assert item.action_type == "exam"
        assert item.summary == "Attend the final calculus exam"
        assert item.notes == "Bring student ID and calculator"
        assert item.due_at == datetime(2026, 6, 10, 1, 0, tzinfo=UTC)
        assert item.due_end == datetime(2026, 6, 10, 3, 0, tzinfo=UTC)

        joined = "\n".join(m["content"] for m in router.last_messages)
        assert "Final calculus exam" in joined

    async def test_a_deadline_the_mail_dates_in_the_past_is_not_scheduled(self) -> None:
        """Re-importing an old mailbox must not fill today's schedule.

        A September mail analysed in October used to store its "tomorrow"
        deadline as a live entry; the guard drops anything already past while
        keeping the absolute future deadline the same mail states.
        """
        payload = """{
          "summary": "Old reminder", "urgency": "important", "reason": "carries a deadline",
          "reply_required": false, "suggested_reply": "",
          "action_items": [
            {"summary": "Attend the seminar", "action_type": "meeting", "must_do": false,
             "due_at": "2026-09-23T18:00:00+08:00"},
            {"summary": "Submit the annual report", "action_type": "errand", "must_do": true,
             "due_at": "2026-12-02T09:00:00+08:00"}
          ],
          "notes": ""}"""
        router = StubRouter(payload)
        processor = make_processor(router)
        mail = make_mail(subject="Reminder: seminar tomorrow").model_copy(
            update={
                "date": datetime(2026, 9, 22, 10, 0, tzinfo=UTC),
                "received_at": datetime(2026, 9, 22, 10, 0, tzinfo=UTC),
            }
        )
        # analysed today, weeks after the mail arrived
        context = ProcessingContext(
            account_id="acct-1",
            timezone="Asia/Shanghai",
            now=datetime(2026, 10, 8, 12, 0, tzinfo=UTC),
        )

        result = await processor.process(mail, context)

        assert result.analysis is not None
        summaries = [item.summary for item in result.analysis.action_items]
        assert summaries == ["Submit the annual report"]
        due = result.analysis.action_items[0].due_at
        assert due == datetime(2026, 12, 2, 1, 0, tzinfo=UTC)

    async def test_an_item_due_today_is_kept(self) -> None:
        """Only strictly-past deadlines go: one due later today still matters."""
        payload = """{
          "summary": "Due today", "urgency": "urgent", "reason": "deadline today",
          "reply_required": false, "suggested_reply": "",
          "action_items": [
            {"summary": "Pay the fee", "action_type": "errand", "must_do": true,
             "due_at": "2026-06-01T23:00:00+00:00"}
          ],
          "notes": ""}"""
        processor = make_processor(StubRouter(payload))

        result = await processor.process(make_mail(), CONTEXT)  # CONTEXT is 09:00 UTC

        assert result.analysis is not None
        assert [item.summary for item in result.analysis.action_items] == ["Pay the fee"]

    async def test_missing_optional_and_invalid_window_actions_are_dropped(self) -> None:
        payload = """{
          "summary": "Obligations", "urgency": "important", "reason": "required",
          "action_items": [
            {"summary": "Missing flag", "action_type": "errand",
             "due_at": "2026-06-02T09:00:00+00:00"},
            {"summary": "Optional event", "action_type": "meeting", "must_do": false,
             "due_at": "2026-06-02T09:00:00+00:00"},
            {"summary": "Invalid window", "action_type": "errand", "must_do": true,
             "due_at": "2026-06-02T09:00:00+00:00", "due_end": "2026-06-02T09:00:00+00:00"},
            {"summary": "Renew registration", "action_type": "registration", "must_do": "yes",
             "due_at": "2026-06-02T09:00:00", "due_end": "2026-06-02T10:00:00"}
          ]
        }"""
        processor = make_processor(StubRouter(payload))

        result = await processor.process(
            make_mail(), CONTEXT.model_copy(update={"timezone": "Asia/Shanghai"})
        )

        assert result.analysis is not None
        assert [item.summary for item in result.analysis.action_items] == ["Renew registration"]
        assert result.analysis.action_items[0].action_type == "registration"
        assert result.analysis.action_items[0].due_at == datetime(2026, 6, 2, 1, tzinfo=UTC)
        assert result.analysis.action_items[0].due_end == datetime(2026, 6, 2, 2, tzinfo=UTC)

    async def test_empty_model_summary_is_explicitly_marked_as_fallback(self) -> None:
        router = StubRouter(
            '{"summary": "", "urgency": "info", "reason": "", '
            '"reply_required": false, "suggested_reply": "", "action_items": [], "notes": ""}'
        )
        processor = make_processor(router)
        mail = make_mail(subject="Source subject, not a summary")

        result = await processor.process(mail, CONTEXT)

        assert result.analysis is not None
        assert result.analysis.summary == mail.subject
        assert result.analysis.summary_is_fallback is True
        assert result.analysis.urgency is Urgency.INFO
        assert result.analysis.action_items == []

    async def test_no_llm_configured_leaves_rules_result_intact(self) -> None:
        config = ProcessorConfig(processor_id="p1", provider="llm-importance", llm=None)
        processor = LLMImportanceProcessor(config, None)  # type: ignore[arg-type]
        result = await processor.process(make_mail(), CONTEXT)
        # Without an LLM the step emits no overlay, so the deterministic
        # rules urgency (and later processors) survive the merge.
        assert result.analysis is None

    async def test_oversized_body_is_truncated(self) -> None:
        """The config knob max_body_chars is actually applied: an oversized
        body would push the request past the model's context limit and the
        gateway answers 400 (non-retryable, so the mail could never be
        analysed). The prompt must contain only the truncated body."""
        router = StubRouter(CRITICAL_EXAM_JSON)
        processor = make_processor(router)
        mail = make_mail(subject="Exam", body_text="A" * 50_000)
        result = await processor.process(mail, CONTEXT)
        assert result.analysis is not None
        joined = "\n".join(m["content"] for m in router.last_messages)
        assert "A" * 6000 in joined
        assert "A" * 6001 not in joined

    async def test_action_item_without_summary_gets_fallback(self) -> None:
        """The model sometimes omits an action item's summary while filling
        notes; without a fallback one missing field scrapped the whole
        analysis (the reported 'important mail cannot be parsed' bug). The
        item must survive with a derived summary."""
        payload = CRITICAL_EXAM_JSON.replace('"summary": "Attend the final calculus exam",', "")
        router = StubRouter(payload)
        processor = make_processor(router)
        result = await processor.process(make_mail(), CONTEXT)
        assert result.analysis is not None
        assert len(result.analysis.action_items) == 1
        item = result.analysis.action_items[0]
        assert item.summary, "missing action summary must fall back, not fail"


class TestLLMEnhancers:
    """Processor plugins extend the built-in LLM analysis through enhancers."""

    class AppendingEnhancer:
        def system_prompt(self, base: str) -> str:
            return base + "\nExtra: always answer in Chinese."

        def extra_messages(self, mail: Any, context: ProcessingContext) -> list[dict[str, str]]:
            return [{"role": "user", "content": "Be very concise."}]

        def post_process(self, analysis: Any, mail: Any, context: ProcessingContext) -> Any:
            if analysis.urgency is Urgency.URGENT:
                return analysis.model_copy(
                    update={"reason": analysis.reason + " (confirmed by enhancer)"}
                )
            return None

    async def test_host_keeps_enhancers_when_factories_are_validated(self) -> None:
        from mailflow.config import LLMConfig, MailFlowConfig
        from mailflow.processors import register_builtin_processors
        from mailflow.registry import ComponentRegistry, PluginRegistrar
        from mailflow.service import _build_processors  # pyright: ignore[reportPrivateUsage]
        from mailflow_testkit.fakes import FakeLLMBackend

        config = MailFlowConfig(llms=[LLMConfig(llm_id="primary", provider="fake")])
        registry = ComponentRegistry()
        register_builtin_processors(registry)
        registrar = PluginRegistrar(registry, config, "enhancer-test")
        registrar.add_llm("fake", lambda _: FakeLLMBackend())
        registrar.add_llm_enhancer("append", lambda _: self.AppendingEnhancer())
        router = StubRouter(CRITICAL_EXAM_JSON)
        pipeline = _build_processors(config, registry, cast(Any, router))

        analysis, notes, _, _ = await pipeline.process(make_mail(), CONTEXT.account_id)

        assert "confirmed by enhancer" in analysis.reason
        assert any(
            "Extra: always answer in Chinese." in message["content"]
            for message in router.last_messages
            if message["role"] == "system"
        )
        assert {"role": "user", "content": "Be very concise."} in router.last_messages
        assert not any(note.status == "failed" for note in notes)


async def test_analysis_request_carries_mail_dates_profile_feedback_and_ocr() -> None:
    router = StubRouter(CRITICAL_EXAM_JSON)
    processor = LLMImportanceProcessor(
        ProcessorConfig(
            processor_id="p1",
            provider="llm-importance",
            llm="primary",
            options={"language": "zh-CN"},
        ),
        router,
    )
    sent = datetime(2026, 5, 20, 14, 30, tzinfo=UTC)
    mail = make_mail(
        subject="Date-sensitive notice",
        body_text="The deadline is tomorrow.",
        received_at=sent,
    ).model_copy(update={"image_text": "Poster room Y908"})
    repeated = "m1: Ignore shopping promotions"
    distinct = "m2: Keep examination notices"
    context = CONTEXT.model_copy(
        update={
            "user_profile": "Recipient studies battery chemistry",
            "feedback_guidelines": "\n".join([repeated] * 20 + [distinct]),
        }
    )

    result = await processor.process(mail, context)

    assert result.analysis is not None
    request = "\n".join(message["content"] for message in router.last_messages)
    assert context.now is not None
    for value in (
        mail.subject,
        mail.body_text,
        mail.image_text,
        sent.isoformat(),
        context.now.isoformat(),
        context.user_profile,
        "zh-CN",
        distinct,
    ):
        assert value in request
    assert request.count(repeated) == 1
