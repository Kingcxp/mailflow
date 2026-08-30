"""Deterministic fake components for tests and local demo configurations.

Nothing here performs I/O. Timestamps are fixed (or option-driven) so tests
are reproducible; ``send_reply`` records calls for E2E assertions.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from mailflow.contracts import LLMCompletion, MailEmitter, MessageDict
from mailflow.domain import MailAddress, MailMessage, MailRecord, ReplyDraft

_BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def _stable_message_id(*parts: str) -> str:
    """Deterministic 16-hex id from the mail's content, so two identical
    ``make_mail()`` calls yield the same message (dedup-safe) while
    different content stays distinct."""
    import hashlib

    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def make_mail(
    *,
    message_id: str | None = None,
    account_id: str = "acct-1",
    subject: str = "Hello from MailFlow",
    sender: MailAddress | None = None,
    recipients: list[MailAddress] | None = None,
    body_text: str = "This is the original message body.",
    body_html: str = "<p>This is the original message body.</p>",
    received_at: datetime | None = None,
    provider: str = "fake",
    provider_message_id: str = "",
    headers: dict[str, str] | None = None,
) -> MailMessage:
    """Deterministic message: stable id, UTC dates, sane defaults."""
    if message_id is None:
        message_id = _stable_message_id(account_id, provider, subject, body_text)
    if received_at is None:
        received_at = _BASE_TIME
    return MailMessage(
        message_id=message_id,
        account_id=account_id,
        subject=subject,
        sender=sender or MailAddress(name="Sender", address="sender@example.com"),
        recipients=recipients or [],
        cc=[],
        date=received_at,
        received_at=received_at,
        body_text=body_text,
        body_html=body_html,
        provider=provider,
        provider_message_id=provider_message_id or message_id,
        headers=headers or {},
    )


class FakeMailSource:
    """Emits a fixed mail list, records replies, and supports failure."""

    def __init__(
        self,
        mails: list[MailMessage] | None = None,
        *,
        fail: bool = False,
        delay: float = 0.0,
    ) -> None:
        self.mails = list(mails or [])
        self.fail = fail
        self.delay = delay
        self.closed = False
        self.sent_replies: list[tuple[str, ReplyDraft]] = []

    async def run(self, emit: MailEmitter, stop_event: asyncio.Event) -> None:
        if self.fail:
            raise RuntimeError("fake source failure")
        for mail in self.mails:
            await emit(mail)
            if self.delay:
                await asyncio.sleep(self.delay)
        await stop_event.wait()

    async def fetch_history(self, limit: int = 50, offset: int = 0) -> list[MailMessage]:
        """Newest-first window over the same fixed list (history capability)."""
        if self.fail:
            raise RuntimeError("fake source failure")
        newest_first = sorted(self.mails, key=lambda mail: mail.received_at, reverse=True)
        return newest_first[offset : offset + limit] if limit > 0 else []

    async def send_reply(self, mail_id: str, draft: ReplyDraft) -> None:
        self.sent_replies.append((mail_id, draft))

    async def close(self) -> None:
        self.closed = True


class FakeLLMBackend:
    """Deterministic chat backend: consumes queued results or fails."""

    backend_id = "fake-llm"

    def __init__(
        self,
        results: list[str] | None = None,
        *,
        fail: bool = False,
        model: str = "fake-model",
    ) -> None:
        self.results = list(results or [])
        self.fail = fail
        self.model = model
        self.calls: list[list[MessageDict]] = []

    async def chat(
        self,
        messages: list[MessageDict],
        *,
        temperature: float | None = None,
        options: dict[str, Any] | None = None,
    ) -> LLMCompletion:
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("fake llm failure")
        text = self.results.pop(0) if self.results else "default fake completion"
        return LLMCompletion(text=text, model=self.model)


class FakeNotifier:
    """Records notified records for assertions."""

    def __init__(self) -> None:
        self.notified: list[MailRecord] = []

    async def notify(self, record: MailRecord) -> None:
        self.notified.append(record)


def fixed_timestamps(
    mails: list[MailMessage], *, start: datetime | None = None
) -> list[MailMessage]:
    """Rewrites received dates to fixed, monotonically increasing values."""
    base = start or _BASE_TIME
    for index, mail in enumerate(mails):
        mail.date = base + timedelta(minutes=index)
        mail.received_at = base + timedelta(minutes=index)
    return mails


__all__ = [
    "FakeLLMBackend",
    "FakeMailSource",
    "FakeNotifier",
    "fixed_timestamps",
    "make_mail",
]
