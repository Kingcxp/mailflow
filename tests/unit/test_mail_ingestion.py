"""Receive acknowledgements, MIME/OCR and retry-safe analysis, without real I/O."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
from unittest.mock import Mock

import pytest
from mailflow.config import MailAccountConfig
from mailflow_mail_imap.plugin import IMAPSource

from mailflow_test_support.runtime import build_runtime


class Mailbox:
    def __init__(self) -> None:
        self.uids = [1, 2, 3]
        self.validity = "10"
        self.fail_uid = 0
        self.closed = 0
        self.raw: bytes | None = None
        self.fetched: list[int] = []

    def login(self, *args: Any) -> None:
        pass

    def select(self, folder: str, *, readonly: bool = False) -> tuple[str, list[bytes]]:
        assert readonly
        return "OK", []

    def response(self, name: str) -> tuple[str, list[bytes]]:
        return "UIDVALIDITY", [self.validity.encode()]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if command == "search":
            return "OK", [" ".join(map(str, self.uids)).encode()]
        uid = int(args[0])
        self.fetched.append(uid)
        assert "PEEK" in args[1]
        if uid == self.fail_uid:
            raise ConnectionError("connection interrupted")
        raw = f"From: a@example.test\r\nMessage-ID: <mail-{uid}>\r\nSubject: mail {uid}\r\n\r\nbody".encode()
        return "OK", [(b'1 (INTERNALDATE "01-Jan-2026 12:00:00 +0000")', self.raw or raw)]

    def logout(self) -> None:
        self.closed += 1


def source_for(monkeypatch: pytest.MonkeyPatch, mailbox: Mailbox, **options: Any) -> Any:
    monkeypatch.setattr("mailflow_mail_imap.plugin.imaplib.IMAP4_SSL", Mock(return_value=mailbox))
    return IMAPSource(
        MailAccountConfig(
            account_id="account",
            provider="imap",
            options={"preset": "gmail", "initial_sync": "all", **options},
        )
    )


async def test_mid_batch_disconnect_preserves_prefix_and_retries_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = Mailbox()
    mailbox.fail_uid = 2
    source = source_for(monkeypatch, mailbox)
    saved: list[str] = []
    source.set_watermark_store(lambda: None, saved.append)
    received: list[str] = []

    async def accept(mail: Any) -> None:
        received.append(mail.subject)

    await source._poll_once(accept)
    assert received == ["mail 1"]
    assert json.loads(saved[-1])["uid"] == 1
    mailbox.fail_uid = 0
    await source._poll_once(accept)
    assert received == ["mail 1", "mail 2", "mail 3"]
    assert json.loads(saved[-1])["uid"] == 3
    assert mailbox.closed == 2


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_queue_handoff_is_not_acknowledged(
    monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    source = source_for(monkeypatch, Mailbox())
    saved: list[str] = []
    source.set_watermark_store(lambda: None, saved.append)
    received: list[str] = []

    async def reject_second(mail: Any) -> None:
        if mail.subject == "mail 2":
            if cancel:
                raise asyncio.CancelledError
            raise RuntimeError("queue rejected")
        received.append(mail.subject)

    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await source._poll_once(reject_second)
    assert json.loads(saved[-1])["uid"] == 1

    async def accept(mail: Any) -> None:
        received.append(mail.subject)

    restarted = source_for(monkeypatch, Mailbox())
    restarted.set_watermark_store(lambda: saved[-1], saved.append)
    await restarted._poll_once(accept)
    assert received == ["mail 1", "mail 2", "mail 3"]


@pytest.mark.parametrize("sync_options", [{}, {"analyze_backlog": True}, {"initial_sync": "new"}])
async def test_startup_skips_old_checkpoints_but_receives_new_arrivals(
    monkeypatch: pytest.MonkeyPatch,
    sync_options: dict[str, Any],
) -> None:
    mailbox = Mailbox()
    monkeypatch.setattr("mailflow_mail_imap.plugin.imaplib.IMAP4_SSL", Mock(return_value=mailbox))
    account = MailAccountConfig(
        account_id="account",
        provider="imap",
        options={"preset": "gmail", "interval_seconds": 0.01, **sync_options},
    )
    source = IMAPSource(account)
    runtime, storage, _ = build_runtime({"account": source}, accounts=[account])
    # This is the user's old backlog checkpoint. Startup must not fetch the
    # next historical page, even after mail records have been cleared.
    await storage.set_preference(
        "imap.watermark.account",
        json.dumps(
            {"uid": 1, "uidvalidity": mailbox.validity, "scope": source._scope, "history_until": 3}  # pyright: ignore[reportPrivateUsage]
        ),
    )

    synced, analyzed = asyncio.Event(), asyncio.Event()

    async def changed(event: str, **payload: Any) -> None:
        if event == "account.sync.changed":
            synced.set()
        elif event == "mailflow.mail.processed":
            analyzed.set()

    runtime._events.subscribe_all(changed)  # pyright: ignore[reportPrivateUsage]

    await runtime.start()
    try:
        await asyncio.wait_for(synced.wait(), timeout=2)
        assert not storage.saved and not mailbox.fetched
        assert (
            json.loads((await storage.get_preference("imap.watermark.account")) or "{}")["uid"] == 3
        )
        # A real restart with the persisted checkpoint must also stay empty.
        await runtime.stop()
        synced.clear()
        await runtime.start()
        await asyncio.wait_for(synced.wait(), timeout=2)
        assert not storage.saved and not mailbox.fetched
        mailbox.uids.append(4)
        await asyncio.wait_for(analyzed.wait(), timeout=2)
        assert len(storage.saved) == 1
        assert await runtime.wait_idle()
        current = next(record for record in storage.saved if record.mail.subject == "mail 4")
        assert current.analysis is not None and current.analysis_state == "ready"
        assert current.ingestion == "live"
        assert mailbox.fetched == [4]
    finally:
        await runtime.stop()


async def test_full_runtime_queue_cancellation_keeps_the_pending_mail_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = Mailbox()
    mailbox.uids = []
    source = source_for(monkeypatch, mailbox)
    runtime, storage, _ = build_runtime({})
    blocked = asyncio.Event()

    class Queue(asyncio.Queue[Any]):
        async def put(self, item: Any) -> None:
            if self.full():
                blocked.set()
            await super().put(item)

    monkeypatch.setattr(runtime, "_queue", Queue(maxsize=1))
    runtime._inject_watermark_store(source, "account")  # pyright: ignore[reportPrivateUsage]
    await source._poll_once(runtime.receive_mail)
    mailbox.uids = [1, 2, 3]
    pending = asyncio.create_task(
        runtime._run_source(source._account, source, runtime._source_generation)  # pyright: ignore[reportPrivateUsage]
    )
    try:
        await asyncio.wait_for(blocked.wait(), timeout=2)
    finally:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    checkpoint = await storage.get_preference("imap.watermark.account")
    assert checkpoint is not None and json.loads(checkpoint)["uid"] == 1
    assert runtime._queue.get_nowait().subject == "mail 1"  # pyright: ignore[reportPrivateUsage]

    restarted = source_for(monkeypatch, Mailbox())
    runtime._inject_watermark_store(restarted, "account")  # pyright: ignore[reportPrivateUsage]
    received: list[str] = []

    async def accept(mail: Any) -> None:
        received.append(mail.subject)

    await restarted._poll_once(accept)
    assert received == ["mail 2", "mail 3"]


async def test_empty_baseline_uid_reset_and_history_are_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = Mailbox()
    mailbox.uids = []
    source = source_for(monkeypatch, mailbox, initial_sync="new")
    received: list[str] = []

    async def accept(mail: Any) -> None:
        received.append(mail.subject)

    await source._poll_once(accept)
    mailbox.uids = [1, 2]
    assert [mail.subject for mail in await source.fetch_history()] == ["mail 2", "mail 1"]
    await source._poll_once(accept)
    assert received == ["mail 1", "mail 2"]
    mailbox.validity = "11"
    mailbox.uids = [1]
    await source._poll_once(accept)
    assert received == ["mail 1", "mail 2"]
    mailbox.uids.append(2)
    await source._poll_once(accept)
    assert received == ["mail 1", "mail 2", "mail 2"]
    assert source._last_uid == 2


async def test_uid_reset_without_message_id_or_date_does_not_duplicate_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = Mailbox()
    mailbox.uids = [1]
    mailbox.raw = b"From: a@example.test\r\nSubject: stable mail\r\n\r\nbody"
    source = source_for(monkeypatch, mailbox)
    runtime, storage, _ = build_runtime({})

    async def accept(mail: Any) -> None:
        await runtime.process_mail_now(mail)

    await source._poll_once(accept)
    mailbox.uids, mailbox.validity = [8], "11"
    await source._poll_once(accept)
    assert len(storage.saved) == 1
    assert source._last_uid == 8


async def test_cancelled_fetch_thread_never_advances_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = Mailbox()
    entered, release = threading.Event(), threading.Event()
    source = source_for(monkeypatch, mailbox)
    original = source._fetch_once

    def blocked() -> Any:
        entered.set()
        release.wait(timeout=3)
        return original()

    monkeypatch.setattr(source, "_fetch_once", blocked)
    task = asyncio.create_task(source._poll_once(Mock()))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    await asyncio.to_thread(lambda: None)
    assert source._last_uid is None
