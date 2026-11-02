"""Receive acknowledgements, MIME/OCR and retry-safe analysis, without real I/O."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from mailflow import ocr
from mailflow.commands import CommandRouter
from mailflow.config import MailAccountConfig
from mailflow.domain import MAX_ATTACHMENT_BYTES, ActionItem, MailAnalysis, MailRecord, Urgency
from mailflow_mail_imap.plugin import IMAPSource, parse_mime

from mailflow_test_support.runtime import FakeStorage, build_runtime, make_mail


class Mailbox:
    def __init__(self) -> None:
        self.uids = [1, 2, 3]
        self.validity = "10"
        self.fail_uid = 0
        self.closed = 0
        self.raw: bytes | None = None

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
            options={"preset": "gmail", "analyze_backlog": True, **options},
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


async def test_full_runtime_queue_cancellation_keeps_the_pending_mail_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = source_for(monkeypatch, Mailbox())
    runtime, storage, _ = build_runtime({})
    blocked = asyncio.Event()

    class Queue(asyncio.Queue[Any]):
        async def put(self, item: Any) -> None:
            if self.full():
                blocked.set()
            await super().put(item)

    runtime._queue = Queue(maxsize=1)  # pyright: ignore[reportPrivateUsage]
    runtime._inject_watermark_store(source, "account")  # pyright: ignore[reportPrivateUsage]
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
    source = source_for(monkeypatch, mailbox, analyze_backlog=False)
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
    assert received == ["mail 1", "mail 2", "mail 1"]
    assert source._last_uid == 1


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


def image_mail(size: int = 2048) -> bytes:
    mime = EmailMessage()
    mime["From"], mime["Subject"], mime["Message-ID"] = "a@example.test", "See poster", "<poster>"
    mime.set_content("Original body")
    mime.add_attachment(b"x" * size, maintype="image", subtype="png", filename="poster.png")
    return mime.as_bytes()


async def test_ocr_receives_real_bytes_and_text_survives_storage_and_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bytes] = []

    def recognize(images: list[Any], **kwargs: Any) -> str:
        seen.extend(image.data for image in images)
        return "Research Se minar room Y908"

    monkeypatch.setattr(ocr, "extract_mail_images", recognize)
    mail = parse_mime(image_mail(), "account", ocr_enabled=True)
    assert seen == [b"x" * 2048]
    assert mail.body_text.strip() == "Original body"
    assert mail.attachments[0].data == b"x" * 2048
    runtime, storage, _ = build_runtime(
        {}, analysis=MailAnalysis(summary="Poster", urgency=Urgency.INFO)
    )
    stored = await runtime.process_mail_now(mail)
    assert stored is not None and stored.mail.image_text == "Research Se minar room Y908"
    assert stored.mail.attachments[0].data is None
    assert CommandRouter._matches_query(stored, "seminar")  # pyright: ignore[reportPrivateUsage]
    assert CommandRouter._matches_query(stored, "y908")  # pyright: ignore[reportPrivateUsage]
    assert (await storage.list_mails())[0].mail.image_text == stored.mail.image_text


def test_ocr_off_failure_and_oversized_attachment_preserve_original_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recognize = Mock(side_effect=RuntimeError("fake OCR unavailable"))
    monkeypatch.setattr(ocr, "extract_mail_images", recognize)
    disabled = parse_mime(image_mail(), "account")
    recognize.assert_not_called()
    failed = parse_mime(image_mail(), "account", ocr_enabled=True)
    assert failed.body_text == disabled.body_text and failed.parse_error == ""
    assert failed.image_text == ""
    oversized = parse_mime(image_mail(MAX_ATTACHMENT_BYTES + 1), "account")
    assert oversized.attachments[0].data is None
    assert oversized.attachments[0].size == MAX_ATTACHMENT_BYTES + 1
    assert ocr.should_attempt("IMAGE/PNG; ignored=yes", 2048, filename="poster.png")
    assert not ocr.should_attempt("image/svg+xml", 2048, filename="poster.svg")


async def test_concurrent_retries_and_reanalysis_preserve_local_state_and_action_identity() -> None:
    mail = make_mail()
    action = ActionItem(
        item_id="random-from-model",
        mail_id="wrong",
        summary="Submit report",
        action_type="task",
        due_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    runtime, storage, _ = build_runtime(
        {},
        workers=2,
        analysis=MailAnalysis(
            summary="valid",
            urgency=Urgency.INFO,
            action_items=[action, action.model_copy(update={"item_id": "other"})],
        ),
    )
    results = await asyncio.gather(*(runtime.process_mail_now(mail) for _ in range(3)))
    assert sum(result is not None for result in results) == 1
    first = storage.saved[0]
    assert len(first.action_items) == 1
    item_id = first.action_items[0].item_id
    assert first.action_items[0].mail_id == first.record_id
    await storage.set_manual_urgency(first.record_id, Urgency.URGENT)
    await storage.set_preference(f"feedback.{first.record_id}", "Keep this important")
    await storage.set_preference(f"reminder.{item_id}.early", "fired")
    repeated = await runtime.process_mail_now(mail, force=True)
    assert repeated is not None and repeated.manual_urgency == Urgency.URGENT
    assert repeated.action_items[0].item_id == item_id
    assert await storage.get_preference(f"feedback.{first.record_id}") == "Keep this important"
    assert await storage.get_preference(f"reminder.{item_id}.early") == "fired"


async def test_legacy_record_is_reused_only_within_its_account() -> None:
    storage = FakeStorage()
    mail = make_mail()
    legacy = MailRecord(record_id=mail.message_id, mail=mail, manual_urgency=Urgency.URGENT)
    await storage.save_mail(legacy)
    runtime, _, _ = build_runtime(
        {}, storage=storage, analysis=MailAnalysis(summary="fresh", urgency=Urgency.INFO)
    )
    assert await runtime.process_mail_now(mail) is None
    stored = await runtime.process_mail_now(mail, force=True)
    assert stored is not None and stored.record_id == legacy.record_id
    assert stored.manual_urgency == Urgency.URGENT
    other = await runtime.process_mail_now(mail.model_copy(update={"account_id": "other"}))
    assert other is not None and other.record_id != legacy.record_id
    assert len(storage.saved) == 2


async def test_configuration_paths_bind_the_same_analysis_and_recover_after_model_deletion(
    tmp_path: Path,
) -> None:
    from mailflow.config import LLMConfig, MailFlowConfig, load_config, write_config
    from mailflow.contracts import LLMCompletion
    from mailflow.plugin_api import define_plugin
    from mailflow.plugins import PluginManager
    from mailflow.service import start_service

    calls: list[str] = []

    class Backend:
        backend_id = "local"

        def __init__(self, config: LLMConfig) -> None:
            self.name = config.llm_id

        async def chat(
            self, messages: Any, *, temperature: Any = None, options: Any = None, tools: Any = None
        ) -> LLMCompletion:
            calls.append(self.name)
            return LLMCompletion(
                text=json.dumps({"summary": f"analyzed by {self.name}", "urgency": "info"})
            )

    def storage_factory(config: Any) -> Any:
        return FakeStorage()

    def manager(config: MailFlowConfig) -> PluginManager:
        plugin = define_plugin("mailflow-local-fixtures")
        plugin.llm("local")(Backend)
        plugin.storage("memory")(storage_factory)
        result = PluginManager(config)
        result.register(plugin.build())
        return result

    path = tmp_path / "mail.toml"
    path.write_text(
        '[storage]\nprovider="memory"\n[[llms]]\nllm_id="primary"\nprovider="local"\n',
        encoding="utf-8",
    )
    loaded = load_config(path)
    startup = MailFlowConfig.model_validate(
        {"storage": {"provider": "memory"}, "llms": [{"llm_id": "primary", "provider": "local"}]}
    )
    assert loaded.processors == startup.processors
    blank = MailFlowConfig.model_validate({"storage": {"provider": "memory"}})
    ui_path = tmp_path / "ui.toml"
    write_config(blank, ui_path)
    service = await start_service(
        blank, ui_path, plugin_manager=manager(blank), enable_logging=False
    )
    try:
        await service.add_config_entry("llms", {"llm_id": "primary", "provider": "local"})
        assert service.config.processors == startup.processors
        assert [binding.processor_id for binding in service.pipeline.bindings] == ["llm-importance"]
        first = await service.process_mail(make_mail())
        assert first is not None and first.summary == "analyzed by primary"
        await service.add_config_entry("llms", {"llm_id": "backup", "provider": "local"})
        await service.remove_config_entry("llms", 0)
        assert service.config.processors[0].llm == "backup"
        result = await service.process_mail(make_mail())
        assert result is not None and result.summary == "analyzed by backup"
        await service.remove_config_entry("llms", 0)
        assert [binding.processor_id for binding in service.pipeline.bindings] == ["rules"]
        result = await service.process_mail(make_mail(subject="No model"))
        assert result is not None and result.summary == "No model"
        assert not any(note.status == "failed" for note in result.processor_notes)
    finally:
        await service.stop()

    unavailable = MailFlowConfig.model_validate(
        {
            "storage": {"provider": "memory"},
            "llms": [
                {"llm_id": "missing", "provider": "uninstalled"},
                {"llm_id": "backup", "provider": "local"},
            ],
        }
    )
    service = await start_service(
        unavailable, ui_path, plugin_manager=manager(unavailable), enable_logging=False
    )
    try:
        record = await service.process_mail(make_mail())
        assert record is not None and record.summary == "analyzed by backup"
        await service.remove_config_entry("llms", 1)
        assert service.config.llms[0].llm_id == "missing"
        assert [binding.processor_id for binding in service.pipeline.bindings] == ["rules"]
        record = await service.process_mail(make_mail(subject="Unavailable backend"))
        assert record is not None and record.summary == "Unavailable backend"
        assert not any(note.status == "failed" for note in record.processor_notes)
    finally:
        await service.stop()


async def test_cancelled_analysis_can_be_retried_without_duplicate_actions() -> None:
    mail = make_mail()
    entered = asyncio.Event()
    release = asyncio.Event()
    action = ActionItem(
        item_id="random",
        mail_id="wrong",
        summary="Submit form",
        action_type="task",
        due_at=datetime(2099, 1, 1, tzinfo=UTC),
    )

    class Pipeline:
        router = None

        async def process(self, mail: Any, account_id: str, **kwargs: Any) -> Any:
            entered.set()
            await release.wait()
            return (
                MailAnalysis(summary="valid", urgency=Urgency.INFO, action_items=[action]),
                [],
                "",
                "",
            )

    runtime, storage, _ = build_runtime(
        {}, analysis=MailAnalysis(summary="unused", urgency=Urgency.INFO)
    )
    runtime._pipeline = Pipeline()  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]
    pending = asyncio.create_task(runtime.process_mail_now(mail))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert storage.saved == []
    release.set()
    record = await runtime.process_mail_now(mail)
    assert record is not None and len(record.action_items) == 1
    assert await runtime.process_mail_now(mail) is None
