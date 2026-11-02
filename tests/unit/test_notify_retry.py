"""Notification retries through the public mail-processing entry point."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mailflow.domain import MailAnalysis, MailRecord, Urgency

from mailflow_test_support.runtime import RecordingNotifier, build_runtime, make_mail


class FlakyNotifier(RecordingNotifier):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.attempts = 0

    async def notify(self, record: MailRecord) -> None:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise RuntimeError("transport unavailable")
        await super().notify(record)


@pytest.mark.parametrize("failures", [1, 99], ids=["transient", "exhausted"])
async def test_notification_failure_keeps_mail_and_other_destinations_working(
    failures: int, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    original_sleep = asyncio.sleep

    async def no_backoff(_delay: float, *args: Any, **kwargs: Any) -> None:
        await original_sleep(0)

    monkeypatch.setattr("mailflow.runtime.asyncio.sleep", no_backoff)
    flaky, healthy = FlakyNotifier(failures), RecordingNotifier()
    runtime, storage, _ = build_runtime(
        {},
        analysis=MailAnalysis(summary="Useful notice", urgency=Urgency.URGENT),
        notifiers=[flaky, healthy],
    )
    mail = make_mail(subject="Notification regression")

    record = await asyncio.wait_for(runtime.process_mail_now(mail), timeout=2)

    assert record is not None
    assert await storage.get_mail(record.record_id) == record
    assert healthy.notified == [record]
    if failures == 1:
        assert flaky.notified == [record]
        assert flaky.attempts > 1
    else:
        assert flaky.notified == []
        assert 1 < flaky.attempts < failures
        assert record.record_id in caplog.text and "notifier" in caplog.text

    assert await runtime.process_mail_now(mail) is None
    assert healthy.notified == [record]
