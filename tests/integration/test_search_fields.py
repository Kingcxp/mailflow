"""Search data passed from the service into model requests, with a scripted model."""

from __future__ import annotations

from typing import Any, cast

from mailflow.config import LLMConfig
from mailflow.contracts import LLMRouter
from mailflow.domain import Attachment

from mailflow_test_support.commands import commands_service, make_record


async def test_smart_model_receives_sender_summary_attachment_and_ocr_text() -> None:
    service = commands_service()
    service.config.llms = [LLMConfig(llm_id="fake")]
    record = make_record()
    record.mail.attachments = [Attachment(filename="attachment-only.png")]
    record.mail.image_text = "OCR-only phrase"
    await service.storage.save_mail(record)
    prompts: list[str] = []

    async def chat(messages: Any, **kwargs: Any) -> Any:
        from mailflow.contracts import LLMCompletion

        prompt = str(messages[-1]["content"])
        prompts.append(prompt)
        return LLMCompletion(
            text="ok" if prompt.startswith("Reply with") else '[{"id":"m1","relevance":90}]'
        )

    service.router = cast(LLMRouter, type("FakeRouter", (), {"chat": staticmethod(chat)})())
    result = await service.smart_rank("attachment-only")
    assert [r.record_id for r in result.records] == [record.record_id]
    assert all(
        value in prompts[-1]
        for value in (
            record.mail.subject,
            record.mail.sender.display,
            record.mail.body_text,
            record.summary,
            "attachment-only.png",
            "OCR-only phrase",
        )
    )
