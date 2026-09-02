"""Unit tests for the Ask & Correct conversational analysis flow."""

from __future__ import annotations

from typing import Any, cast

import pytest
from mailflow.config import LLMConfig, MailFlowConfig
from mailflow.contracts import LLMCompletion
from mailflow.domain import MailAnalysis, MailRecord, Urgency
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine
from mailflow.registry import ComponentRegistry
from mailflow.service import MailFlowService


def _make_record() -> MailRecord:
    """A stored mail with an existing analysis; body must never change."""
    from mailflow_testkit.fakes import make_mail

    mail = make_mail(
        subject="Important meeting tomorrow",
        body_text="Please confirm your attendance before noon tomorrow.",
    )
    return MailRecord(
        record_id="ask-1",
        mail=mail,
        auto_urgency=Urgency.INFO,
        analysis=MailAnalysis(
            summary="A meeting notice",
            urgency=Urgency.INFO,
            reason="general notice",
        ),
    )


class _CorrectionRouter:
    """Returns a canned reply with a [c]...[/c] correction block."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.seen: list[dict[str, str]] = []

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMCompletion:
        self.seen = messages
        return LLMCompletion(text=self._reply, llm_id="llm-1", backend="fake")


class _FakePluginManager:
    """Minimal plugin-manager stub satisfying MailFlowService.__init__."""

    def snapshots(self, registry: ComponentRegistry) -> list[Any]:
        return []

    def build_registry(self) -> ComponentRegistry:
        return ComponentRegistry()

    def enabled_infos(self) -> list[Any]:
        return []


def _service(config: MailFlowConfig | None = None) -> MailFlowService:
    return MailFlowService(
        config=config or MailFlowConfig(),
        registry=ComponentRegistry(),
        plugin_manager=cast(Any, _FakePluginManager()),
        storage=cast(Any, _MemoryStorage()),
        sources={},
        router=cast(Any, _CorrectionRouter("")),
        pipeline=PipelineEngine([]),
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        i18n=I18n(),
    )


class _MemoryStorage:
    """Minimal in-memory storage supporting the Ask & Correct flow."""

    def __init__(self) -> None:
        self.mails: dict[str, MailRecord] = {}
        self.preferences: dict[str, str] = {}

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def save_mail(self, record: MailRecord) -> None:
        self.mails[record.record_id] = record

    async def get_mail(self, record_id: str) -> MailRecord | None:
        return self.mails.get(record_id)

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
            from mailflow.domain import MailAnalysis as MA

            analysis = MA(
                summary=summary or record.mail.subject,
                urgency=urgency or record.auto_urgency,
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

    async def get_preference(self, key: str) -> str | None:
        return self.preferences.get(key)

    async def set_preference(self, key: str, value: str) -> None:
        self.preferences[key] = value


def _config_with_llm() -> MailFlowConfig:
    return MailFlowConfig(
        llms=[
            LLMConfig(
                llm_id="llm-1",
                name="fake",
                provider="fake",
                model="fake-model",
                default=True,
            )
        ]
    )


@pytest.mark.asyncio
async def test_chat_about_mail_applies_correction_and_keeps_body() -> None:
    """A [c] block updates urgency/summary/reason but never the mail body."""
    router = _CorrectionRouter(
        "You are right, this is important.\n"
        "[c]\n"
        '{"urgency": "important", "summary": "Confirm meeting", "reason": "reply required"}\n'
        "[/c]"
    )
    service = _service(_config_with_llm())
    service.router = cast(Any, router)  # type: ignore[attr-defined]
    await service.storage.save_mail(_make_record())

    result = await service.chat_about_mail(
        "ask-1",
        [{"role": "user", "content": "This is important, please raise it."}],
    )

    assert result["corrections"]["urgency"] == "important"
    assert "[c]" not in result["reply"]
    record = await service.storage.get_mail("ask-1")
    assert record is not None
    assert record.auto_urgency == Urgency.IMPORTANT
    assert record.analysis is not None
    assert record.analysis.urgency == Urgency.IMPORTANT
    assert record.analysis.summary == "Confirm meeting"
    assert record.analysis.reason == "reply required"
    # original mail untouched
    assert record.mail.subject == "Important meeting tomorrow"
    assert "Please confirm your attendance" in (record.mail.body_text or "")

    # the user's disagreement became a lasting guideline
    guidelines = await service.feedback_guidelines()
    assert "raise it" in guidelines
    assert "ask-1" in guidelines


@pytest.mark.asyncio
async def test_chat_about_mail_no_correction_no_guideline() -> None:
    """A plain reply without a [c] block changes nothing."""
    router = _CorrectionRouter("No, info is correct because it is optional.")
    service = _service(_config_with_llm())
    service.router = cast(Any, router)  # type: ignore[attr-defined]
    await service.storage.save_mail(_make_record())

    result = await service.chat_about_mail(
        "ask-1", [{"role": "user", "content": "Why is this info?"}]
    )

    assert result["corrections"] == {}
    record = await service.storage.get_mail("ask-1")
    assert record is not None
    assert record.auto_urgency == Urgency.INFO
    assert await service.feedback_guidelines() == ""


@pytest.mark.asyncio
async def test_chat_about_mail_no_llm_configured() -> None:
    """Without an LLM the flow returns a friendly notice, no crash."""
    service = _service()  # no LLMs in config
    await service.storage.save_mail(_make_record())

    result = await service.chat_about_mail("ask-1", [{"role": "user", "content": "Any question"}])

    assert "No LLM is configured" in result["reply"]
    assert result["corrections"] == {}


@pytest.mark.asyncio
async def test_chat_about_mail_unknown_mail() -> None:
    service = _service(_config_with_llm())
    result = await service.chat_about_mail("missing", [{"role": "user", "content": "Hi"}])
    assert "not found" in result["reply"]
