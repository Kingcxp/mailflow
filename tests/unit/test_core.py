"""Unit tests for the MailFlow core domain contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mailflow.domain import (
    MailAnalysis,
    MailMessage,
    MailRecord,
    Urgency,
    parse_urgency,
)

ADDRESS = {
    "name": "Sender",
    "address": "sender@example.com",
}


def make_mail(**overrides) -> MailMessage:
    base = {
        "message_id": "msg-1",
        "account_id": "acct-1",
        "subject": "Hello",
        "sender": ADDRESS,
        "recipients": [],
        "cc": [],
        "date": "2026-01-01T10:00:00+00:00",
        "received_at": "2026-01-01T10:05:00+00:00",
        "body_text": "body",
        "body_html": "<p>body</p>",
        "provider": "fake",
    }
    base.update(overrides)
    return MailMessage.model_validate(base)


class TestUrgencyContract:
    """The four-level urgency contract is public and fixed."""

    def test_exactly_four_values(self) -> None:
        assert len(Urgency) == 4

    def test_values(self) -> None:
        assert Urgency.AD.value == "ad"
        assert Urgency.INFO.value == "info"
        assert Urgency.IMPORTANT.value == "important"
        assert Urgency.URGENT.value == "urgent"

    def test_colors(self) -> None:
        assert Urgency.AD.color == "#909399"  # gray: irrelevant ads
        assert Urgency.INFO.color == "#67C23A"  # green: useful, not urgent
        assert Urgency.IMPORTANT.color == "#E6A23C"  # orange: important, read it
        assert Urgency.URGENT.color == "#F56C6C"  # red: must handle

    def test_rank_order(self) -> None:
        assert Urgency.AD.rank < Urgency.INFO.rank < Urgency.IMPORTANT.rank < Urgency.URGENT.rank

    def test_parse_synonyms(self) -> None:
        assert parse_urgency("urgent") is Urgency.URGENT
        assert parse_urgency("critical") is Urgency.URGENT
        assert parse_urgency("junk") is Urgency.AD
        assert parse_urgency("ads") is Urgency.AD
        assert parse_urgency("medium") is Urgency.IMPORTANT
        assert parse_urgency("bogus-value") is Urgency.INFO
        assert parse_urgency(None) is Urgency.INFO


class TestMailRecordUrgency:
    def test_effective_defaults_to_automatic(self) -> None:
        record = MailRecord(
            record_id="r1",
            mail=make_mail(),
            auto_urgency=Urgency.IMPORTANT,
        )
        assert record.effective_urgency is Urgency.IMPORTANT

    def test_manual_wins_over_automatic(self) -> None:
        record = MailRecord(
            record_id="r1",
            mail=make_mail(),
            auto_urgency=Urgency.AD,
            manual_urgency=Urgency.URGENT,
        )
        assert record.effective_urgency is Urgency.URGENT

    def test_reset_restores_automatic(self) -> None:
        record = MailRecord(
            record_id="r1",
            mail=make_mail(),
            auto_urgency=Urgency.IMPORTANT,
            manual_urgency=Urgency.URGENT,
        )
        assert record.effective_urgency is Urgency.URGENT
        record.manual_urgency = None
        assert record.effective_urgency is Urgency.IMPORTANT

    def test_manual_never_overwrites_automatic(self) -> None:
        record = MailRecord(
            record_id="r1",
            mail=make_mail(),
            auto_urgency=Urgency.URGENT,
            manual_urgency=Urgency.AD,
        )
        assert record.auto_urgency is Urgency.URGENT
        assert record.effective_urgency is Urgency.AD

    def test_summary_falls_back_to_subject(self) -> None:
        record = MailRecord(record_id="r1", mail=make_mail(), auto_urgency=Urgency.INFO)
        assert record.summary == "Hello"

    def test_summary_from_analysis(self) -> None:
        record = MailRecord(
            record_id="r1",
            mail=make_mail(),
            auto_urgency=Urgency.INFO,
            analysis=MailAnalysis(summary="the summary", urgency=Urgency.INFO),
        )
        assert record.summary == "the summary"

    def test_serialization_roundtrip(self) -> None:
        record = MailRecord(
            record_id="r1",
            mail=make_mail(),
            auto_urgency=Urgency.URGENT,
            manual_urgency=Urgency.IMPORTANT,
            analysis=MailAnalysis(summary="s", urgency=Urgency.URGENT, reason="r"),
        )
        restored = MailRecord.model_validate_json(record.model_dump_json())
        assert restored == record
        assert restored.effective_urgency is Urgency.IMPORTANT


class TestMailMessage:
    def test_normalized_message_id_uses_provider_id(self) -> None:
        mail = make_mail(provider_message_id="provider-42")
        assert mail.normalized_message_id() == "provider-42"

    def test_normalized_message_id_digest_without_provider_id(self) -> None:
        mail = make_mail()
        digest = mail.normalized_message_id()
        assert len(digest) == 24
        assert mail.normalized_message_id() == digest  # stable

    def test_invalid_urgency_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MailAnalysis(summary="x", urgency="not-a-value")
