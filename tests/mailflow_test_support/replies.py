"""Local fixtures shared across isolated tests and component integration tests."""

from __future__ import annotations

import asyncio

from mailflow.domain import ReplyDraft

from mailflow_test_support.service import RecordingSource
from mailflow_test_support.service import service as service


class WaitingSource(RecordingSource):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send_reply(self, mail_id: str, draft: ReplyDraft) -> None:
        self.started.set()
        await self.release.wait()
        await super().send_reply(mail_id, draft)
