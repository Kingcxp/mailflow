"""Unit tests for the LLM importance processor and JSON extraction."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from mailflow.config import ProcessorConfig
from mailflow.contracts import LLMCompletion, ProcessingContext
from mailflow.domain import Urgency
from mailflow.processors import (
    LLMImportanceProcessor,
    extract_json,
    parse_due_at,
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
    def test_plain_json(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self) -> None:
        text = 'Here is the result:\n```json\n{"a": 2}\n```\nHope that helps.'
        assert extract_json(text) == {"a": 2}

    def test_prose_wrapped_json_brace_slice(self) -> None:
        text = 'Sure, the answer is {"urgency": "urgent"} done.'
        assert extract_json(text) == {"urgency": "urgent"}

    def test_unparseable_raises(self) -> None:
        with pytest.raises((json.JSONDecodeError, ValueError)):
            extract_json("no json here")


class TestParseDueAt:
    def test_aware_passthrough(self) -> None:
        value = parse_due_at("2026-06-10T09:00:00+08:00", "UTC")
        assert value.utcoffset() is not None
        assert value.hour == 9

    def test_naive_gets_context_timezone(self) -> None:
        value = parse_due_at("2026-06-10T09:00:00", "Asia/Shanghai")
        assert value.tzinfo is not None
        assert value.utcoffset().total_seconds() == 8 * 3600  # type: ignore[union-attr]


class TestLLMImportanceProcessor:
    async def test_critical_exam_with_student_id(self) -> None:
        router = StubRouter(CRITICAL_EXAM_JSON)
        processor = make_processor(router)
        mail = make_mail(
            subject="Final calculus exam", body_text="Bring your student ID to the exam hall."
        )
        result = await processor.process(mail, CONTEXT)

        assert result.analysis is not None
        analysis = result.analysis
        assert analysis.urgency is Urgency.URGENT
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
        assert item.due_at.tzinfo is not None

        # prompt carries the four-level semantics and the mail content
        joined = "\n".join(m["content"] for m in router.last_messages)
        assert "important" in joined and "urgent" in joined
        assert "Final calculus exam" in joined

    async def test_urgency_case_and_synonym_normalized(self) -> None:
        payload = CRITICAL_EXAM_JSON.replace('"urgent"', '"Critical"')
        router = StubRouter(payload)
        processor = make_processor(router)
        result = await processor.process(make_mail(), CONTEXT)
        assert result.analysis is not None
        assert result.analysis.urgency is Urgency.URGENT

    async def test_no_action_items(self) -> None:
        router = StubRouter(
            '{"summary": "Just a notice", "urgency": "info", "reason": "", '
            '"reply_required": false, "suggested_reply": "", "action_items": [], "notes": ""}'
        )
        processor = make_processor(router)
        result = await processor.process(make_mail(), CONTEXT)
        assert result.analysis is not None
        assert result.analysis.urgency is Urgency.INFO
        assert result.analysis.action_items == []

    async def test_no_llm_configured_leaves_rules_result_intact(self) -> None:
        config = ProcessorConfig(processor_id="p1", provider="llm-importance", llm=None)
        processor = LLMImportanceProcessor(config, None)  # type: ignore[arg-type]
        result = await processor.process(make_mail(), CONTEXT)
        # Without an LLM the step emits no overlay, so the deterministic
        # rules urgency (and later processors) survive the merge.
        assert result.analysis is None


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

    async def test_enhancer_prompt_messages_and_output(self) -> None:
        router = StubRouter(CRITICAL_EXAM_JSON)
        config = ProcessorConfig(
            processor_id="p1",
            provider="llm-importance",
            llm="primary",
            fallback_llms=["backup"],
        )
        processor = LLMImportanceProcessor(config, router, enhancers=[self.AppendingEnhancer()])
        result = await processor.process(make_mail(), CONTEXT)
        # system prompt appended
        assert any(
            "Extra: always answer in Chinese." in m["content"]
            for m in router.last_messages
            if m["role"] == "system"
        )
        # extra message injected after the user message
        assert {"role": "user", "content": "Be very concise."} in router.last_messages
        # post-processing adjusted the parsed analysis
        assert result.analysis is not None
        assert "confirmed by enhancer" in result.analysis.reason

    async def test_no_enhancers_unchanged(self) -> None:
        router = StubRouter(CRITICAL_EXAM_JSON)
        processor = make_processor(router)
        result = await processor.process(make_mail(), CONTEXT)
        assert result.analysis is not None
        assert "confirmed by enhancer" not in result.analysis.reason
        assert len(router.last_messages) == 2  # system + user only


class TestSummaryLanguage:
    async def test_language_option_injects_instruction(self) -> None:
        router = StubRouter(CRITICAL_EXAM_JSON)
        config = ProcessorConfig(
            processor_id="p1",
            provider="llm-importance",
            llm="primary",
            options={"language": "zh-CN"},
        )
        processor = LLMImportanceProcessor(config, router)
        await processor.process(make_mail(), CONTEXT)
        user_message = next(m for m in router.last_messages if m["role"] == "user")
        assert "zh-CN" in user_message["content"]
        assert "Write the summary" in user_message["content"]

    async def test_no_language_leaves_messages_unchanged(self) -> None:
        router = StubRouter(CRITICAL_EXAM_JSON)
        processor = make_processor(router)
        await processor.process(make_mail(), CONTEXT)
        user_message = next(m for m in router.last_messages if m["role"] == "user")
        assert "Write the summary" not in user_message["content"]
