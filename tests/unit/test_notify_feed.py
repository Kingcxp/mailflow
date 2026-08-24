"""The LLM tab notification feed renders one colored entry per processed
mail using the record's effective urgency and mail subject."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mailflow.domain import MailAnalysis, MailMessage, MailRecord, Urgency
from mailflow_tui.settings import _NotifyFeed


class _Pane:
    def __init__(self) -> None:
        self.rendered = 0

    def _render_notify_feed(self) -> None:
        self.rendered += 1

    @property
    def app(self) -> Any:  # pragma: no cover - call_later path is suppressed
        raise RuntimeError("no app in unit test")


def _record(urgency: Urgency) -> MailRecord:
    from mailflow.domain import MailAddress

    mail = MailMessage(
        message_id="<n@e>",
        account_id="acct-1",
        sender=MailAddress(address="a@example.com"),
        recipients=[],
        subject="考试安排",
        body_text="下周三考试",
        date=datetime.now(UTC),
        received_at=datetime.now(UTC),
    )
    return MailRecord(
        record_id="r1",
        mail=mail,
        auto_urgency=urgency,
        analysis=MailAnalysis(summary="周三考试", urgency=urgency),
    )


async def test_feed_renders_effective_urgency_and_subject() -> None:
    feed = _NotifyFeed(service=Any, pane=_Pane())  # type: ignore[arg-type]
    await feed._on_processed(record=_record(Urgency.URGENT))

    entries = feed.snapshot()
    assert len(entries) == 1
    text = entries[0].plain
    assert "URGENT" in text
    assert "考试安排" in text
    assert "周三考试" in text


async def test_feed_ignores_missing_record() -> None:
    feed = _NotifyFeed(service=Any, pane=_Pane())  # type: ignore[arg-type]
    await feed._on_processed()
    assert feed.snapshot() == []
