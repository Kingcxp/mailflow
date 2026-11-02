"""SQLite persistence, deletion, restoration and restart behavior against temporary databases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mailflow.config import StorageConfig
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

    async def test_user_delete_can_refresh_the_trash_window(
        self, storage: SQLiteStorage, tmp_path: Path
    ) -> None:
        """An explicit purge restarts retention for a mail already in the trash.

        Preserving the first deletion timestamp is right for the automatic
        cleanup, but it would let a mail the user was just told is restorable
        be purged for good minutes later.
        """
        import sqlite3

        record = make_record("rewindow")
        await storage.save_mail(record)
        await storage.delete_mail("rewindow")
        # simulate the both-stores state with an ancient deletion timestamp
        stale = (utcnow() - timedelta(days=30)).timestamp()
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE trash_records SET deleted_ts = ? WHERE record_id = ?",
            (stale, "rewindow"),
        )
        conn.commit()
        conn.close()
        await storage.save_mail(record)
        await storage.delete_mail("rewindow", refresh_deleted_at=True)

        # the refreshed entry survives a cleanup that would have purged it
        assert await storage.purge_trash(utcnow() - timedelta(days=7)) == 0
        assert len(await storage.list_trash()) == 1

        # the automatic path still preserves the first timestamp
        await storage.restore_from_trash("rewindow")
        await storage.delete_mail("rewindow")
        preserved = (await storage.list_trash())[0].deleted_at
        assert preserved >= utcnow() - timedelta(minutes=1)

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
