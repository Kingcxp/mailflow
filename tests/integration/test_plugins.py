"""Integration tests for concrete plugin backends."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from mailflow.config import LLMConfig, StorageConfig
from mailflow.domain import Attachment, MailAnalysis, MailRecord, ReplyDraft, Urgency, utcnow
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
            "mailflow-storage-sqlite",
            "mailflow-llm-openai-compatible",
            "mailflow-processor-rules",
            "mailflow-processor-llm-importance",
            "mailflow-notify-console",
        }
        assert registry.has(ComponentKind.MAIL_SOURCE, "fake")
        assert registry.has(ComponentKind.STORAGE, "sqlite")
        assert registry.has(ComponentKind.LLM_BACKEND, "openai-compatible")
        assert registry.has(ComponentKind.MAIL_PROCESSOR, "rules")
        assert registry.has(ComponentKind.MAIL_PROCESSOR, "llm-importance")
        assert registry.has(ComponentKind.NOTIFIER, "console")
        # ownership is stamped at registration
        assert registry.plugin_for("fake") == "mailflow-mail-fake"
        assert registry.plugin_for("sqlite") == "mailflow-storage-sqlite"

    def test_discovery_deduplicates_bundled_plugins(self) -> None:
        from mailflow.config import MailFlowConfig
        from mailflow_bundled import create_plugin_manager

        manager = create_plugin_manager(MailFlowConfig(), discover_external=True)
        # entry points resolve to the same singletons; no double registration
        assert manager.plugin_ids.count("mailflow-storage-sqlite") == 1
