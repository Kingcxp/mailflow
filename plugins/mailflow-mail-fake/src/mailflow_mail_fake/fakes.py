"""Self-contained deterministic mail source used by the fake provider.

Mirrors ``mailflow_testkit.fakes.FakeMailSource`` so the plugin installs with
only ``mailflow-core``: nothing here performs I/O and replies are merely
recorded.
"""

from __future__ import annotations

import asyncio

from mailflow.contracts import MailEmitter
from mailflow.domain import MailMessage, ReplyDraft


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


__all__ = ["FakeMailSource"]
