"""Unit tests for the event bus, LLM router and processing pipeline."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from mailflow.config import LLMConfig
from mailflow.contracts import (
    LLMCompletion,
    MailMessage,
    ProcessingContext,
    ProcessorDecision,
    ProcessorResult,
)
from mailflow.domain import MailAddress, MailAnalysis, Urgency
from mailflow.events import EventBus
from mailflow.llm import LLMRouteError, LLMRouterImpl
from mailflow.pipeline import PipelineEngine, ProcessorBinding, merge_analysis

ADDRESS = MailAddress(name="Sender", address="sender@example.com")


def make_mail(subject: str = "Hello") -> MailMessage:
    return MailMessage(
        message_id=uuid.uuid4().hex[:16],
        account_id="acct-1",
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


class FakeLLM:
    """Deterministic backend: fails N times then succeeds, or always."""

    backend_id = "fake"

    def __init__(self, results: list[str] | None = None, fail: bool = False) -> None:
        self.results = list(results or [])
        self.fail = fail
        self.calls: list[list[dict[str, str]]] = []

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        options: dict[str, Any] | None = None,
    ) -> LLMCompletion:
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("backend down")
        if self.results:
            return LLMCompletion(text=self.results.pop(0), model="fake-model")
        return LLMCompletion(text="default", model="fake-model")


class FailingLLM:
    backend_id = "failing"

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        options: dict[str, Any] | None = None,
    ) -> LLMCompletion:
        raise RuntimeError("boom")


class RecordingProcessor:
    def __init__(self, processor_id: str, result: ProcessorResult) -> None:
        self.processor_id = processor_id
        self.result = result
        self.calls = 0

    async def process(self, mail: MailMessage, context: ProcessingContext) -> ProcessorResult:
        self.calls += 1
        return self.result


class FlakyProcessor:
    """Fails ``failures`` times, then succeeds."""

    def __init__(self, processor_id: str, failures: int) -> None:
        self.processor_id = processor_id
        self.failures = failures
        self.calls = 0

    async def process(self, mail: MailMessage, context: ProcessingContext) -> ProcessorResult:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("flaky")
        return ProcessorResult(analysis=MailAnalysis(summary="recovered", urgency=Urgency.INFO))


class TestEventBus:
    async def test_subscribe_and_emit(self) -> None:
        bus = EventBus()
        seen: list[dict[str, Any]] = []

        async def record(event: str, **payload: Any) -> None:
            seen.append(payload)

        bus.subscribe("mail.new", record)
        await bus.emit("mail.new", mail_id="m1")
        assert seen == [{"mail_id": "m1"}]

    async def test_wildcard_subscription(self) -> None:
        bus = EventBus()
        seen: list[str] = []

        async def record(event: str, **payload: Any) -> None:
            seen.append(event)

        bus.subscribe_all(record)
        await bus.emit("anything", x=1)
        assert seen == ["anything"]

    async def test_failing_handler_does_not_break_others(self) -> None:
        bus = EventBus()
        good: list[str] = []

        async def bad(event: str, **payload: Any) -> None:
            raise RuntimeError("handler bug")

        async def fine(event: str, **payload: Any) -> None:
            good.append("ran")

        bus.subscribe("x", bad)
        bus.subscribe("x", fine)
        await bus.emit("x")
        assert good == ["ran"]

    async def test_unsubscribe(self) -> None:
        bus = EventBus()
        seen: list[str] = []

        async def record(event: str, **payload: Any) -> None:
            seen.append("hit")

        unsubscribe = bus.subscribe("x", record)
        await bus.emit("x")
        unsubscribe()
        await bus.emit("x")
        assert seen == ["hit"]


class TestLLMRouter:
    async def test_primary_success_stamps_identity(self) -> None:
        backend = FakeLLM(results=["ok"])
        router = LLMRouterImpl(
            backends={"primary": backend},
            configs={
                "primary": LLMConfig(
                    llm_id="primary", base_url="https://a", model="m1", provider="fake"
                ),
            },
        )
        completion = await router.chat([{"role": "user", "content": "hi"}], primary="primary")
        assert completion.text == "ok"
        assert completion.llm_id == "primary"
        assert completion.backend == "fake"

    async def test_primary_failure_falls_back(self) -> None:
        failing = FailingLLM()
        backup = FakeLLM(results=["backup answer"])
        router = LLMRouterImpl(
            backends={"p": failing, "b": backup},
            configs={
                "p": LLMConfig(llm_id="p", base_url="https://a", model="m1", provider="failing"),
                "b": LLMConfig(llm_id="b", base_url="https://b", model="m2", provider="fake"),
            },
        )
        completion = await router.chat(
            [{"role": "user", "content": "hi"}], primary="p", fallback=["b"]
        )
        assert completion.text == "backup answer"
        assert completion.llm_id == "b"
        assert completion.backend == "fake"

    async def test_all_backends_fail_raises(self) -> None:
        router = LLMRouterImpl(
            backends={"p": FailingLLM()},
            configs={
                "p": LLMConfig(llm_id="p", base_url="https://a", model="m1", provider="failing")
            },
        )
        with pytest.raises(LLMRouteError):
            await router.chat([{"role": "user", "content": "hi"}], primary="p")

    async def test_dedup_repeated_ids(self) -> None:
        backend = FakeLLM(results=["x"])
        router = LLMRouterImpl(
            backends={"p": backend},
            configs={"p": LLMConfig(llm_id="p", base_url="https://a", model="m1", provider="fake")},
        )
        await router.chat([{"role": "user", "content": "hi"}], primary="p", fallback=["p"])
        assert len(backend.calls) == 1

    async def test_error_redacts_api_key(self) -> None:
        secret = "sk-super-secret-123"

        class SecretFailingLLM(FailingLLM):
            async def chat(self, *args: Any, **kwargs: Any) -> LLMCompletion:
                raise RuntimeError(f"request to https://x?key={secret} failed")

        router = LLMRouterImpl(
            backends={"p": SecretFailingLLM()},
            configs={
                "p": LLMConfig(
                    llm_id="p", base_url="https://a", model="m1", api_key=secret, provider="failing"
                )
            },
        )
        with pytest.raises(LLMRouteError) as excinfo:
            await router.chat([{"role": "user", "content": "hi"}], primary="p")
        assert secret not in str(excinfo.value)
        assert "***" in str(excinfo.value)


class TestPipeline:
    def _binding(self, processor: Any, processor_id: str = "p1", **kw: Any) -> ProcessorBinding:
        defaults: dict[str, Any] = dict(
            priority=100, processor_id=processor_id, plugin_id="test-plugin"
        )
        defaults.update(kw)
        return ProcessorBinding(processor=processor, **defaults)

    async def test_ordered_execution(self) -> None:
        order: list[str] = []

        class P:
            processor_id = "p"

            async def process(
                self, mail: MailMessage, context: ProcessingContext
            ) -> ProcessorResult:
                order.append(self.processor_id)
                return ProcessorResult(
                    analysis=MailAnalysis(summary=f"s-{self.processor_id}", urgency=Urgency.INFO)
                )

        first = P()
        first.processor_id = "first"
        second = P()
        second.processor_id = "second"
        engine = PipelineEngine(
            [
                self._binding(second, "second", priority=200),
                self._binding(first, "first", priority=100),
            ]
        )
        analysis, notes, _, _ = await engine.process(make_mail(), "acct-1")
        assert order == ["first", "second"]
        assert analysis.summary == "s-second"  # later processor wins
        assert [n.processor_id for n in notes] == ["first", "second"]
        assert all(n.status == "success" for n in notes)

    async def test_retry_then_success(self) -> None:
        flaky = FlakyProcessor("flaky", failures=2)
        engine = PipelineEngine([self._binding(flaky, "flaky", retries=3)])
        analysis, notes, _, _ = await engine.process(make_mail(), "acct-1")
        assert flaky.calls == 3
        assert analysis.summary == "recovered"
        assert notes[0].status == "success"

    async def test_continue_after_retries_exhausted(self) -> None:
        """Failure with policy=continue must still run the next processor."""
        failing = FlakyProcessor("failing", failures=99)
        fallback = RecordingProcessor(
            "fallback",
            ProcessorResult(analysis=MailAnalysis(summary="saved", urgency=Urgency.INFO)),
        )
        engine = PipelineEngine(
            [self._binding(failing, "failing", retries=2), self._binding(fallback, "fallback")]
        )
        analysis, notes, _, _ = await engine.process(make_mail(), "acct-1")
        assert failing.calls == 3  # initial + 2 retries
        assert fallback.calls == 1
        assert analysis.summary == "saved"
        failed_notes = [n for n in notes if n.status == "failed"]
        assert len(failed_notes) == 1
        assert "failed" in failed_notes[0].message

    async def test_stop_policy_halts_pipeline(self) -> None:
        failing = FlakyProcessor("failing", failures=99)
        later = RecordingProcessor(
            "later", ProcessorResult(analysis=MailAnalysis(summary="never", urgency=Urgency.INFO))
        )
        engine = PipelineEngine(
            [
                self._binding(failing, "failing", retries=0, failure_policy="stop"),
                self._binding(later, "later"),
            ]
        )
        analysis, notes, _, _ = await engine.process(make_mail(), "acct-1")
        assert later.calls == 0
        assert analysis.summary == "Hello"  # fallback summary still guaranteed
        assert notes[0].status == "failed"

    async def test_timeout_marks_failed_and_continues(self) -> None:
        class Slow:
            processor_id = "slow"

            async def process(
                self, mail: MailMessage, context: ProcessingContext
            ) -> ProcessorResult:
                await asyncio.sleep(5)
                return ProcessorResult()

        engine = PipelineEngine([self._binding(Slow(), "slow", timeout_seconds=0.05, retries=0)])
        _, notes, _, _ = await engine.process(make_mail(), "acct-1")
        assert notes[0].status == "failed"
        assert "timeout" in notes[0].message or "failed" in notes[0].message

    async def test_fallback_summary_guarantee(self) -> None:
        """No processor produces a summary -> subject-based fallback, recorded."""
        noop = RecordingProcessor("noop", ProcessorResult())
        engine = PipelineEngine([self._binding(noop, "noop")])
        analysis, notes, _, _ = await engine.process(make_mail(subject="Exam notice"), "acct-1")
        assert analysis.summary == "Exam notice"
        assert any(n.processor_id == "pipeline" for n in notes)

    async def test_processor_stop_decision(self) -> None:
        stopper = RecordingProcessor("stopper", ProcessorResult(decision=ProcessorDecision.STOP))
        later = RecordingProcessor(
            "later", ProcessorResult(analysis=MailAnalysis(summary="nope", urgency=Urgency.INFO))
        )
        engine = PipelineEngine(
            [self._binding(stopper, "stopper", priority=50), self._binding(later, "later")]
        )
        analysis, _, _, _ = await engine.process(make_mail(), "acct-1")
        assert later.calls == 0
        assert analysis.summary == "Hello"  # fallback applies after stop


class TestMergeAnalysis:
    def test_overlay_wins_per_field(self) -> None:
        base = MailAnalysis(
            summary="old", urgency=Urgency.INFO, reason="old-reason", reply_required=False
        )
        overlay = MailAnalysis(summary="new", urgency=Urgency.URGENT, reason="new-reason")
        merged = merge_analysis(base, overlay)
        assert merged.summary == "new"
        assert merged.urgency is Urgency.URGENT
        assert merged.reason == "new-reason"
        assert merged.reply_required is False  # overlay explicitly false wins

    def test_empty_overlay_fields_keep_base(self) -> None:
        base = MailAnalysis(summary="keep", urgency=Urgency.IMPORTANT, reason="why", notes="note")
        overlay = MailAnalysis(summary="", urgency=Urgency.INFO, reason="")
        merged = merge_analysis(base, overlay)
        assert merged.summary == "keep"
        assert merged.reason == "why"
        assert merged.notes == "note"
