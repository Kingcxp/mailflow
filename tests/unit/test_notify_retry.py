"""Urgent notifications must survive transient transport failures: the
runtime retries each notifier up to three times before giving up."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from mailflow.config import NotifierConfig
from mailflow.domain import MailAnalysis, MailMessage, MailRecord, Urgency
from mailflow.runtime import MailFlowRuntime


class _FlakyNotifier:
    backend_id = "flaky"

    def __init__(self, failures: int) -> None:
        self._failures = failures
        self.calls = 0

    async def notify(self, record: MailRecord) -> None:
        self.calls += 1
        if self.calls <= self._failures:
            raise RuntimeError("transient transport error")


class _Events:
    async def emit(self, event: str, **payload: Any) -> None: ...


def _record() -> MailRecord:
    mail = MailMessage(
        message_id="m-notify",
        account_id="acct-1",
        subject="考试提醒",
        sender=cast(Any, None)
        or __import__("mailflow.domain", fromlist=["MailAddress"]).MailAddress(address="a@e"),
        recipients=[],
        date=datetime.now(UTC),
        received_at=datetime.now(UTC),
    )
    return MailRecord(
        record_id="m-notify",
        mail=mail,
        auto_urgency=Urgency.URGENT,
        analysis=MailAnalysis(summary="s", urgency=Urgency.URGENT),
    )


def _runtime(notifier: Any) -> MailFlowRuntime:
    runtime = MailFlowRuntime.__new__(MailFlowRuntime)
    runtime._notifiers = [notifier]  # pyright: ignore[reportPrivateUsage]
    runtime._notifier_configs = [  # pyright: ignore[reportPrivateUsage]
        NotifierConfig(notifier_id="n", provider="flaky", minimum_urgency=Urgency.INFO)
    ]  # pyright: ignore[reportPrivateUsage]
    runtime._events = _Events()  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    return runtime


async def test_retry_succeeds_on_second_attempt() -> None:
    import asyncio

    notifier = _FlakyNotifier(failures=1)
    runtime = _runtime(notifier)
    sleep_log: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_log.append(seconds)

    original_sleep = asyncio.sleep
    asyncio.sleep = _fake_sleep  # type: ignore[assignment]
    try:
        await runtime._notify(_record())  # pyright: ignore[reportPrivateUsage]  # pyright: ignore[reportPrivateUsage]
    finally:
        asyncio.sleep = original_sleep  # type: ignore[assignment]
    assert notifier.calls == 2
    assert sleep_log == [2.0]


async def test_gives_up_after_three_attempts() -> None:
    notifier = _FlakyNotifier(failures=99)
    runtime = _runtime(notifier)
    await runtime._notify(_record())  # pyright: ignore[reportPrivateUsage]  # pyright: ignore[reportPrivateUsage]
    assert notifier.calls == 3
