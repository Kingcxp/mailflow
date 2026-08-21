"""Integration tests for concrete plugin backends."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from mailflow.config import LLMConfig, StorageConfig
from mailflow.domain import (
    ActionItem,
    Attachment,
    MailAnalysis,
    MailRecord,
    ReplyDraft,
    Urgency,
    utcnow,
)
from mailflow_storage_sqlite.plugin import SQLiteStorage
from mailflow_testkit.fakes import make_mail


@pytest.fixture
async def storage(tmp_path: Path):
    backend = SQLiteStorage(StorageConfig(path=str(tmp_path / "test.db")))
    await backend.initialize()
    yield backend
    await backend.close()


def make_record(record_id: str = "r1", *, urgency: Urgency = Urgency.IMPORTANT) -> MailRecord:
    return MailRecord(
        record_id=record_id,
        mail=make_mail(message_id=record_id, subject=f"Subject {record_id}"),
        auto_urgency=urgency,
    )


class TestSQLiteStorage:
    async def test_save_and_load_roundtrip(self, storage: SQLiteStorage) -> None:
        record = make_record()
        await storage.save_mail(record)
        loaded = await storage.get_mail("r1")
        assert loaded is not None
        assert loaded == record
        assert await storage.count_mails() == 1

    async def test_list_orders_by_recency(self, storage: SQLiteStorage) -> None:
        old = make_record("old")
        old.received_at = utcnow() - timedelta(days=5)
        new = make_record("new")
        await storage.save_mail(old)
        await storage.save_mail(new)
        records = await storage.list_mails()
        assert [r.record_id for r in records] == ["new", "old"]

    async def test_attachment_payload_not_persisted(self, storage: SQLiteStorage) -> None:
        record = make_record()
        record.mail.attachments.append(
            Attachment(filename="a.bin", content_type="x", size=3, data=b"abc")
        )
        await storage.save_mail(record)
        loaded = await storage.get_mail("r1")
        assert loaded is not None
        assert loaded.mail.attachments[0].filename == "a.bin"
        assert loaded.mail.attachments[0].data is None

    async def test_manual_urgency_and_reset(self, storage: SQLiteStorage) -> None:
        await storage.save_mail(make_record())
        updated = await storage.set_manual_urgency("r1", Urgency.URGENT)
        assert updated is not None
        assert updated.effective_urgency is Urgency.URGENT
        loaded = await storage.get_mail("r1")
        assert loaded is not None
        assert loaded.effective_urgency is Urgency.URGENT
        assert loaded.auto_urgency is Urgency.IMPORTANT  # automatic preserved
        await storage.set_manual_urgency("r1", None)
        restored = await storage.get_mail("r1")
        assert restored is not None
        assert restored.effective_urgency is Urgency.IMPORTANT

    async def test_delete_moves_full_record_to_trash(self, storage: SQLiteStorage) -> None:
        record = make_record()
        record.analysis = MailAnalysis(summary="s", urgency=Urgency.URGENT)
        await storage.save_mail(record)
        await storage.delete_mail("r1")
        assert await storage.get_mail("r1") is None
        trash = await storage.list_trash()
        assert len(trash) == 1
        item = trash[0]
        assert item.record_id == "r1"
        assert item.mail.subject == "Subject r1"
        assert item.analysis is not None and item.analysis.summary == "s"

    async def test_restore_recovers_identical_record(self, storage: SQLiteStorage) -> None:
        record = make_record()
        record.manual_urgency = Urgency.AD
        record.processor_notes = []  # keep simple
        await storage.save_mail(record)
        await storage.delete_mail("r1")
        restored = await storage.restore_from_trash("r1")
        assert restored is not None
        assert restored.record_id == "r1"
        assert restored.effective_urgency is Urgency.AD
        assert restored.mail.subject == "Subject r1"
        fetched = await storage.get_mail("r1")
        assert fetched is not None and fetched == restored
        assert await storage.list_trash() == []

    async def test_restore_unknown_id_returns_none(self, storage: SQLiteStorage) -> None:
        assert await storage.restore_from_trash("ghost") is None

    async def test_purge_compares_trash_timestamp_not_receipt(self, storage: SQLiteStorage) -> None:
        old_receipt = utcnow() - timedelta(days=60)
        record = make_record("ancient")
        record.received_at = old_receipt
        await storage.save_mail(record)
        await storage.delete_mail("ancient")  # deleted just now
        # purge based on receipt age would remove it; trash age must keep it
        purged = await storage.purge_trash(utcnow() - timedelta(days=7))
        assert purged == 0
        assert len(await storage.list_trash()) == 1
        # purge by deletion age removes it
        purged = await storage.purge_trash(utcnow() + timedelta(days=1))
        assert purged == 1
        assert await storage.list_trash() == []

    async def test_cleanup_moves_old_active_mail(self, storage: SQLiteStorage) -> None:
        old = make_record("old")
        old.received_at = utcnow() - timedelta(days=40)
        fresh = make_record("fresh")
        await storage.save_mail(old)
        await storage.save_mail(fresh)
        moved = await storage.cleanup_mail(utcnow() - timedelta(days=30))
        assert moved == 1
        assert await storage.get_mail("old") is None
        assert await storage.get_mail("fresh") is not None
        assert len(await storage.list_trash()) == 1

    async def test_retrash_preserves_first_deletion_time(self, storage: SQLiteStorage) -> None:
        record = make_record("r1")
        record.received_at = utcnow() - timedelta(days=40)
        await storage.save_mail(record)
        moved = await storage.cleanup_mail(utcnow() - timedelta(days=30))
        assert moved == 1
        first_deleted = (await storage.list_trash())[0].deleted_at
        # the same record reappears (re-sync) and cleanup runs again:
        # the first deletion timestamp must survive, keeping the 7-day window
        await storage.save_mail(record)
        moved = await storage.cleanup_mail(utcnow() - timedelta(days=30))
        assert moved == 1
        assert (await storage.list_trash())[0].deleted_at == first_deleted

    async def test_drafts_and_preferences(self, storage: SQLiteStorage) -> None:
        from mailflow.domain import MailAddress

        draft = ReplyDraft(
            draft_id="d1",
            mail_id="r1",
            account_id="acct-1",
            to=MailAddress(address="x@example.com"),
            subject="Re",
            body="body",
        )
        await storage.save_draft(draft)
        loaded = await storage.get_draft("d1")
        assert loaded is not None and loaded == draft
        await storage.delete_draft("d1")
        assert await storage.get_draft("d1") is None

        await storage.set_preference("language", "zh-CN")
        assert await storage.get_preference("language") == "zh-CN"
        assert await storage.get_preference("missing") is None


# ---------------------------------------------------------------------------
# OpenAI-compatible LLM backend (monkeypatched httpx — never a real request)
# ---------------------------------------------------------------------------


class FakeHTTPResponse:
    def __init__(self, status_code: int, payload: dict[str, Any], reason: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.reason_phrase = reason

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("POST", "https://fake")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)


class FakeAsyncClient:
    """Records POSTs; returns a canned response or raises."""

    instances: ClassVar[list[FakeAsyncClient]] = []

    def __init__(self, *, error: Exception | None = None, **kwargs: object) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error
        FakeAsyncClient.instances.append(self)

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> FakeHTTPResponse:
        if self.error is not None:
            raise self.error
        self.calls.append({"url": url, "headers": headers, "params": params, "json": json})
        return FakeHTTPResponse(
            200,
            {
                "choices": [{"message": {"content": "the answer"}}],
                "model": "m1",
            },
        )


def make_llm_config(**overrides: object) -> LLMConfig:
    base: dict[str, object] = {
        "llm_id": "l1",
        "base_url": "https://host/v1",
        "model": "m1",
    }
    base.update(overrides)
    return LLMConfig.model_validate(base)


class TestOpenAICompatibleBackend:
    def test_request_construction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx
        from mailflow_llm_openai_compatible.plugin import OpenAICompatibleBackend

        FakeAsyncClient.instances.clear()
        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
        backend = OpenAICompatibleBackend(
            make_llm_config(
                api_key="sk-secret",
                extra_body={"temperature": 0.2},
                headers={"X-Custom": "yes"},
                query={"api-version": "2024"},
            )
        )
        import asyncio

        completion = asyncio.run(
            backend.chat(
                [{"role": "user", "content": "hi"}],
                temperature=0.9,
                options={"body": {"temperature": 0.7}},
            )
        )
        call = FakeAsyncClient.instances[-1].calls[0]
        assert call["url"] == "https://host/v1/chat/completions"
        assert call["headers"]["Authorization"] == "Bearer sk-secret"
        assert call["headers"]["X-Custom"] == "yes"
        assert call["params"] == {"api-version": "2024"}
        body = call["json"]
        assert body["model"] == "m1"
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        assert body["temperature"] == 0.7  # per-call option wins over extra_body
        assert completion.text == "the answer"
        assert completion.model == "m1"

    def test_no_bearer_without_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        import httpx
        from mailflow_llm_openai_compatible.plugin import OpenAICompatibleBackend

        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
        backend = OpenAICompatibleBackend(make_llm_config())
        asyncio.run(backend.chat([{"role": "user", "content": "hi"}]))
        call = FakeAsyncClient.instances[-1].calls[0]
        assert "Authorization" not in call["headers"]

    def test_retries_exhausted_raises_sanitized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        import httpx
        from mailflow_llm_openai_compatible.plugin import OpenAICompatibleBackend

        FakeAsyncClient.instances.clear()

        def client_factory(**kwargs: object) -> FakeAsyncClient:
            return FakeAsyncClient(error=httpx.ConnectError("boom"))

        monkeypatch.setattr(httpx, "AsyncClient", client_factory)
        backend = OpenAICompatibleBackend(make_llm_config(max_retries=2, api_key="sk-topsecret"))
        with pytest.raises(RuntimeError, match="transport error"):
            asyncio.run(backend.chat([{"role": "user", "content": "hi"}]))
        # exactly max_retries + 1 attempts, and no secret/url in the message
        assert len(FakeAsyncClient.instances) == 3

    def test_http_error_status_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        import httpx
        from mailflow_llm_openai_compatible.plugin import OpenAICompatibleBackend

        class FailingResponse(FakeHTTPResponse):
            def raise_for_status(self) -> None:
                request = httpx.Request("POST", "https://host/secret-path?key=topsecret")
                response = httpx.Response(401, request=request)
                raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

        class Client(FakeAsyncClient):
            async def post(self, *args: object, **kwargs: object) -> FakeHTTPResponse:
                return FailingResponse(401, {})

        monkeypatch.setattr(httpx, "AsyncClient", Client)
        backend = OpenAICompatibleBackend(make_llm_config(api_key="topsecret"))
        with pytest.raises(RuntimeError) as excinfo:
            asyncio.run(backend.chat([{"role": "user", "content": "hi"}]))
        assert "topsecret" not in str(excinfo.value)
        assert "401" in str(excinfo.value)


class TestBundledRegistration:
    """The composition package registers every official component."""

    def test_all_builtin_plugins_registered(self) -> None:
        from mailflow.config import MailFlowConfig
        from mailflow.domain import ComponentKind
        from mailflow_bundled import create_plugin_manager

        manager = create_plugin_manager(MailFlowConfig(), discover_external=False)
        registry = manager.build_registry()
        assert set(manager.plugin_ids) == {
            "mailflow-mail-fake",
            "mailflow-mail-imap",
            "mailflow-storage-sqlite",
            "mailflow-llm-openai-compatible",
            "mailflow-llm-anthropic",
            "mailflow-notify-console",
            "mailflow-export-nonebot",
            "mailflow-export-astrbot",
        }
        assert registry.has(ComponentKind.MAIL_SOURCE, "fake")
        assert registry.has(ComponentKind.MAIL_SOURCE, "imap")
        assert registry.has(ComponentKind.STORAGE, "sqlite")
        assert registry.has(ComponentKind.LLM_BACKEND, "openai-compatible")
        assert registry.has(ComponentKind.LLM_BACKEND, "anthropic")
        assert registry.has(ComponentKind.NOTIFIER, "console")
        # rules/llm-importance are built into the core, not plugin-provided;
        # start_service registers them (covered by the e2e service tests)
        assert registry.has(ComponentKind.BOT_EXPORTER, "nonebot")
        assert registry.has(ComponentKind.BOT_EXPORTER, "astrbot")
        # ownership is stamped at registration
        assert registry.plugin_for("fake") == "mailflow-mail-fake"
        assert registry.plugin_for("sqlite") == "mailflow-storage-sqlite"
        assert registry.plugin_for("nonebot") == "mailflow-export-nonebot"
        assert registry.plugin_for("astrbot") == "mailflow-export-astrbot"

    def test_discovery_deduplicates_bundled_plugins(self) -> None:
        from mailflow.config import MailFlowConfig
        from mailflow_bundled import create_plugin_manager

        manager = create_plugin_manager(MailFlowConfig(), discover_external=True)
        # entry points resolve to the same singletons; no double registration
        assert manager.plugin_ids.count("mailflow-storage-sqlite") == 1


class TestSQLiteCustomActions:
    async def test_custom_action_roundtrip_and_delete(self, storage: SQLiteStorage) -> None:
        item = ActionItem(
            item_id="u1",
            mail_id="",
            summary="Water the plants",
            action_type="errand",
            due_at=datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
            notes="balcony",
        )
        await storage.save_custom_action(item)
        assert await storage.list_custom_actions() == [item]
        # overwrite by id
        updated = item.model_copy(update={"summary": "Water the garden"})
        await storage.save_custom_action(updated)
        assert await storage.list_custom_actions() == [updated]
        assert await storage.delete_custom_action("u1") is True
        assert await storage.list_custom_actions() == []
        assert await storage.delete_custom_action("u1") is False

    async def test_custom_action_persists_across_restart(self, tmp_path: Path) -> None:
        from mailflow.config import StorageConfig

        path = tmp_path / "actions.db"
        first = SQLiteStorage(StorageConfig(provider="sqlite", path=str(path)))
        await first.initialize()
        item = ActionItem(
            item_id="u2",
            mail_id="",
            summary="Renew passport",
            action_type="errand",
            due_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
            notes="",
        )
        await first.save_custom_action(item)
        await first.close()

        second = SQLiteStorage(StorageConfig(provider="sqlite", path=str(path)))
        await second.initialize()
        assert await second.list_custom_actions() == [item]
        await second.close()


class TestTelegramNotifier:
    async def test_skips_without_credentials(self) -> None:
        from mailflow.config import NotifierConfig
        from mailflow_notify_telegram.plugin import TelegramNotifier

        notifier = TelegramNotifier(NotifierConfig(notifier_id="tg", provider="telegram"))
        await notifier.notify(make_record())
        # no exception, nothing sent

    async def test_posts_to_bot_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from urllib.request import Request

        from mailflow.config import NotifierConfig
        from mailflow_notify_telegram.plugin import TelegramNotifier

        sent: dict[str, str] = {}

        class FakeResponse:
            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

            def read(self) -> bytes:
                return b"{}"

        def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
            sent["url"] = request.full_url
            data = request.data
            sent["body"] = data.decode("utf-8") if isinstance(data, bytes) else ""
            return FakeResponse()

        import importlib

        tg_plugin = importlib.import_module("mailflow_notify_telegram.plugin")
        monkeypatch.setattr(tg_plugin, "urlopen", fake_urlopen)  # pyright: ignore[reportUnknownMemberType]
        notifier = TelegramNotifier(
            NotifierConfig(
                notifier_id="tg",
                provider="telegram",
                options={"bot_token": "secret-token", "chat_id": "42"},
            )
        )
        record = make_record(urgency=Urgency.URGENT)
        record.mail.subject = "Exam tomorrow"
        await notifier.notify(record)
        from urllib.parse import unquote_plus

        assert "botsecret-token/sendMessage" in sent["url"]
        assert "chat_id=42" in sent["body"]
        assert "Exam tomorrow" in unquote_plus(sent["body"])
        assert "secret-token" not in unquote_plus(sent["body"])

    def test_stays_identical_to_the_marketplace_copy(self) -> None:
        """The marketplace owns this plugin; the workspace copy exists only so
        the tests above can run. Two copies that drift apart mean the
        marketplace ships behavior nobody tested."""
        from pathlib import Path

        workspace = (
            Path(__file__).resolve().parents[2]
            / "plugins/mailflow-notify-telegram/src/mailflow_notify_telegram/plugin.py"
        )
        marketplace = (
            workspace.parents[4].parent
            / "mailflow-repo/notifier/mailflow-notify-telegram"
            / "src/mailflow_notify_telegram/plugin.py"
        )
        if not marketplace.is_file():
            pytest.skip("marketplace checkout not present")
        assert workspace.read_text(encoding="utf-8") == marketplace.read_text(encoding="utf-8"), (
            "plugins/mailflow-notify-telegram has drifted from the marketplace copy; "
            "sync them or delete the workspace copy together with these tests"
        )


class TestIMAPMailSource:
    def test_parse_mime_basic(self) -> None:
        from mailflow_mail_imap.plugin import parse_mime

        raw = (
            b"From: Alice <alice@example.com>\r\n"
            b"To: me@example.com\r\n"
            b"Subject: =?utf-8?B?5Lya6K6u6YCa55+l?=\r\n"  # 会议通知
            b"Message-ID: <abc-123@example.com>\r\n"
            b"Date: Mon, 10 Jun 2026 09:00:00 +0800\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"Hello body\r\n"
        )
        mail = parse_mime(raw, account_id="acct-1")
        assert mail.message_id == "abc-123@example.com"
        assert mail.sender.address == "alice@example.com"
        assert mail.sender.name == "Alice"
        assert mail.subject == "会议通知"
        assert mail.body_text == "Hello body"
        assert mail.account_id == "acct-1"
        assert mail.date.utcoffset() is not None

    def test_parse_mime_html_preferred(self) -> None:
        from mailflow_mail_imap.plugin import parse_mime

        raw = (
            b"From: a@example.com\r\n"
            b"To: b@example.com\r\n"
            b"Subject: HTML mail\r\n"
            b"Content-Type: multipart/alternative; boundary=x\r\n"
            b"\r\n"
            b"--x\r\nContent-Type: text/plain\r\n\r\nplain text\r\n"
            b"--x\r\nContent-Type: text/html\r\n\r\n<p>html text</p>\r\n"
            b"--x--\r\n"
        )
        mail = parse_mime(raw, account_id="acct-1")
        assert mail.body_text == "plain text"
        assert "<p>html text</p>" in mail.body_html

    async def test_send_reply_via_smtp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mailflow.config import MailAccountConfig
        from mailflow_mail_imap.plugin import IMAPSource

        sent: list[dict[str, Any]] = []

        class FakeSMTP:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                sent.append({"args": args, "kwargs": kwargs, "mail": None})

            def starttls(self) -> None:
                sent[-1]["starttls"] = True

            def login(self, user: str, password: str) -> None:
                sent[-1]["login"] = (user, password)

            def send_message(self, message: Any) -> None:
                sent[-1]["mail"] = message

            def quit(self) -> None:
                pass

        monkeypatch.setattr("mailflow_mail_imap.plugin.smtplib.SMTP_SSL", FakeSMTP)
        account = MailAccountConfig(
            account_id="acct-1",
            provider="imap",
            email="me@example.com",
            options={"preset": "qq", "username": "me@qq.com", "password": "secret"},
        )
        source = IMAPSource(account)
        from mailflow.domain import MailAddress, ReplyDraft

        draft = ReplyDraft(
            draft_id="d1",
            mail_id="m1",
            account_id="acct-1",
            to=MailAddress(address="them@example.com"),
            subject="Re: Hi",
            body="<p>Dear them,</p>",
        )
        await source.send_reply("m1", draft)
        assert sent and sent[0]["login"] == ("me@qq.com", "secret")
        assert sent[0]["mail"]["To"] == "them@example.com"
        assert "Dear them" in str(sent[0]["mail"].get_body())

    async def test_fetch_once_polls_inbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mailflow.config import MailAccountConfig
        from mailflow_mail_imap.plugin import IMAPSource

        class FakeIMAP:
            uids: ClassVar[list[int]] = [1, 2]

            def __init__(self, host: str, port: int) -> None:
                self.host = host
                self.port = port
                self.logged = False
                self.selected = ""

            def login(self, user: str, password: str) -> None:
                self.logged = (user, password)

            def select(self, folder: str) -> None:
                self.selected = folder

            def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
                if command == "search":
                    return "OK", [b" ".join(str(u).encode() for u in self.uids)]
                assert command == "fetch"
                uid = int(args[0])
                raw = (
                    f"From: a@example.com\r\nSubject: msg {uid}\r\n"
                    f"Message-ID: <x-{uid}@e>\r\n"
                    "Date: Mon, 10 Jun 2026 09:00:00 +0800\r\n\r\nbody"
                ).encode()
                return "OK", [(b"1 (UID %d RFC822)" % uid, raw)]

            def logout(self) -> None:
                pass

        monkeypatch.setattr("mailflow_mail_imap.plugin.imaplib.IMAP4_SSL", FakeIMAP)
        account = MailAccountConfig(
            account_id="acct-1",
            provider="imap",
            email="me@qq.com",
            options={
                "preset": "qq",
                "username": "me@qq.com",
                "password": "secret",
                "limit": 5,
            },
        )
        source = IMAPSource(account)
        fetched = source._fetch_once()  # pyright: ignore[reportPrivateUsage]
        assert len(fetched) == 2
        assert fetched[0].subject == "msg 1"
        # incremental poll: no new mails -> nothing fetched
        fetched_again = source._fetch_once()  # pyright: ignore[reportPrivateUsage]
        assert fetched_again == []
        # a burst beyond the first-poll window is still picked up via UIDs
        FakeIMAP.uids = [1, 2, 3, 4, 5]
        fetched_burst = source._fetch_once()  # pyright: ignore[reportPrivateUsage]
        assert [m.subject for m in fetched_burst] == ["msg 3", "msg 4", "msg 5"]

    async def test_failed_fetch_does_not_skip_the_mail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transient FETCH failure must leave the UID watermark behind the
        mail so the next poll retries it instead of losing it forever."""
        from mailflow.config import MailAccountConfig
        from mailflow_mail_imap.plugin import IMAPSource

        state = {"fail_uid": 2}

        class FlakyIMAP:
            def __init__(self, host: str, port: int) -> None: ...

            def login(self, user: str, password: str) -> None: ...

            def select(self, folder: str) -> None: ...

            def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
                if command == "search":
                    return "OK", [b"1 2 3"]
                uid = int(args[0])
                if uid == state["fail_uid"]:
                    return "NO", [None]  # server hiccup on this uid only
                raw = (
                    f"From: a@example.com\r\nSubject: msg {uid}\r\n"
                    f"Message-ID: <x-{uid}@e>\r\n\r\nbody"
                ).encode()
                return "OK", [(b"1", raw)]

            def logout(self) -> None: ...

        monkeypatch.setattr("mailflow_mail_imap.plugin.imaplib.IMAP4_SSL", FlakyIMAP)
        account = MailAccountConfig(
            account_id="acct-1",
            provider="imap",
            options={"preset": "qq", "username": "u", "password": "p", "limit": 5},
        )
        source = IMAPSource(account)
        first = source._fetch_once()  # pyright: ignore[reportPrivateUsage]
        assert [m.subject for m in first] == ["msg 1"]

        state["fail_uid"] = 0  # server recovers
        second = source._fetch_once()  # pyright: ignore[reportPrivateUsage]
        assert [m.subject for m in second] == ["msg 2", "msg 3"]

    async def test_fetch_history_is_newest_first_and_independent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """History browsing must not consume the live stream watermark."""
        from mailflow.config import MailAccountConfig
        from mailflow_mail_imap.plugin import IMAPSource

        class HistoryIMAP:
            def __init__(self, host: str, port: int) -> None: ...

            def login(self, user: str, password: str) -> None: ...

            def select(self, folder: str) -> None: ...

            def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
                if command == "search":
                    return "OK", [b"1 2 3 4 5"]
                uid = int(args[0])
                raw = (
                    f"From: a@example.com\r\nSubject: msg {uid}\r\n"
                    f"Message-ID: <x-{uid}@e>\r\n\r\nbody"
                ).encode()
                return "OK", [(b"1", raw)]

            def logout(self) -> None: ...

        monkeypatch.setattr("mailflow_mail_imap.plugin.imaplib.IMAP4_SSL", HistoryIMAP)
        account = MailAccountConfig(
            account_id="acct-1",
            provider="imap",
            options={"preset": "qq", "username": "u", "password": "p", "limit": 2},
        )
        source = IMAPSource(account)
        page = await source.fetch_history(limit=2)
        assert [m.subject for m in page] == ["msg 5", "msg 4"]
        assert [m.subject for m in await source.fetch_history(limit=2, offset=2)] == [
            "msg 3",
            "msg 2",
        ]
        # browsing left the poll watermark untouched: the live stream still
        # delivers the most recent ``limit`` mails on its first poll
        assert [m.subject for m in source._fetch_once()] == [  # pyright: ignore[reportPrivateUsage]
            "msg 4",
            "msg 5",
        ]


class TestAnthropicBackend:
    async def test_chat_uses_messages_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mailflow.config import LLMConfig
        from mailflow_llm_anthropic.plugin import AnthropicBackend

        captured: list[dict[str, Any]] = []

        class FakeClient:
            def __init__(self, **kwargs: object) -> None:
                pass

            async def __aenter__(self) -> FakeClient:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

            async def post(self, url: str, **kwargs: Any) -> FakeResponse:
                captured.append({"url": url, **kwargs})
                return FakeResponse(
                    {
                        "content": [{"type": "text", "text": "the answer"}],
                        "model": "claude-3-5-sonnet",
                    }
                )

        class FakeResponse:
            def __init__(self, payload: dict[str, Any]) -> None:
                self._payload = payload
                self.status_code = 200
                self.text = ""

            def json(self) -> dict[str, Any]:
                return self._payload

        monkeypatch.setattr("mailflow_llm_anthropic.plugin.httpx.AsyncClient", FakeClient)
        backend = AnthropicBackend(
            LLMConfig(
                llm_id="claude",
                provider="anthropic",
                model="claude-3-5-sonnet",
                api_key="sk-ant-secret",
                base_url="https://api.anthropic.com/v1/messages",
            )
        )
        completion = await backend.chat(
            [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Summarize this mail."},
            ]
        )
        assert completion.text == "the answer"
        assert completion.model == "claude-3-5-sonnet"
        body = captured[0]["json"]
        assert body["model"] == "claude-3-5-sonnet"
        assert body["system"] == "Be concise."
        assert body["messages"] == [{"role": "user", "content": "Summarize this mail."}]
        assert captured[0]["headers"]["x-api-key"] == "sk-ant-secret"
        assert captured[0]["headers"]["anthropic-version"] == "2023-06-01"
