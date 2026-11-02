from __future__ import annotations

from typing import Any

from mailflow.config import LLMConfig
from mailflow.domain import Attachment, MailRecord
from mailflow.ocr import searchable_mail_text
from mailflow_testkit.fakes import make_mail
from mailflow_tui.search import build_search_matcher

from mailflow_test_support.commands import commands_service, make_record


def test_literal_search_is_case_insensitive_by_default() -> None:
    matcher = build_search_matcher("Mail")
    assert matcher.matches("new MAIL message")
    assert not matcher.matches("message")


def test_regex_and_case_sensitive_modes() -> None:
    regex = build_search_matcher(r"m[a-z]+l", regex=True)
    assert regex.matches("Mail")
    assert regex.matches("MAIL")
    sensitive_regex = build_search_matcher(r"m[a-z]+l", regex=True, case_sensitive=True)
    assert sensitive_regex.matches("mail")
    assert not sensitive_regex.matches("MAIL")


def test_invalid_regex_is_reported_without_raising() -> None:
    matcher = build_search_matcher("[", regex=True)
    assert matcher.error
    assert not matcher.matches("anything")


def test_empty_query_matches_all() -> None:
    matcher = build_search_matcher("")
    assert matcher.error is None
    assert matcher.matches("anything")


def test_mail_body_and_image_text_preserve_case_sensitive_search() -> None:
    record = MailRecord(
        record_id="search",
        mail=make_mail(body_text="", body_html="<b>Room</b> Y908").model_copy(
            update={"image_text": "Research Seminar"}
        ),
    )
    text = searchable_mail_text(record.mail)
    assert build_search_matcher("Room Y908", case_sensitive=True).matches(text)
    assert build_search_matcher("Research", case_sensitive=True).matches(text)
    assert not build_search_matcher("research", case_sensitive=True).matches(text)


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

    service.router = type("FakeRouter", (), {"chat": staticmethod(chat)})()
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
