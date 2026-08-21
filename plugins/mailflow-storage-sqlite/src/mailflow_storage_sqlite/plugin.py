"""SQLite storage backend.

- single connection guarded by an asyncio lock; WAL mode + busy timeout
- full domain records serialized to JSON columns; attachment payloads are
  stripped before persisting (original mail text/HTML remains intact)
- manual urgency by reserializing the stored record
- deletion (manual or retention cleanup) moves the *full* record to trash
  with a deletion timestamp; restore returns the identical record
- purge compares the trash deletion timestamp, never the receipt time
- first-deletion timestamps are preserved (INSERT OR IGNORE) across
  restore → re-trash cycles
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailflow.config import StorageConfig
from mailflow.domain import (
    ActionItem,
    ComponentKind,
    MailRecord,
    ReplyDraft,
    TrashRecord,
    Urgency,
)
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

logger = logging.getLogger("mailflow.storage.sqlite")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mails (
    record_id   TEXT PRIMARY KEY,
    record_json TEXT NOT NULL,
    received_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trash_records (
    record_id   TEXT PRIMARY KEY,
    record_json TEXT NOT NULL,
    deleted_ts  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    draft_id   TEXT PRIMARY KEY,
    draft_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custom_actions (
    item_id   TEXT PRIMARY KEY,
    item_json TEXT NOT NULL
);
"""


class SQLiteStorage:
    def __init__(self, config: StorageConfig) -> None:
        self._path = config.path or ":memory:"
        self._options = config.options
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # -- serialization helpers -------------------------------------------------------

    @staticmethod
    def _without_payload(record: MailRecord) -> MailRecord:
        """Deep copy with attachment payloads stripped for persistence."""
        copy = record.model_copy(deep=True)
        for attachment in copy.mail.attachments:
            attachment.data = None
        return copy

    # -- lifecycle ---------------------------------------------------------------

    async def initialize(self) -> None:
        async with self._lock:
            if self._path != ":memory:":
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _check_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("storage not initialized")
        return self._conn

    # -- active mail ------------------------------------------------------------------

    async def save_mail(self, record: MailRecord) -> None:
        stored = self._without_payload(record)
        async with self._lock:
            conn = self._check_conn()
            conn.execute(
                "INSERT OR REPLACE INTO mails (record_id, record_json, received_ts) VALUES (?, ?, ?)",
                (record.record_id, stored.model_dump_json(), record.received_at.timestamp()),
            )
            conn.commit()

    async def get_mail(self, record_id: str) -> MailRecord | None:
        async with self._lock:
            conn = self._check_conn()
            row = conn.execute(
                "SELECT record_json FROM mails WHERE record_id = ?", (record_id,)
            ).fetchone()
        return MailRecord.model_validate_json(row[0]) if row else None

    async def list_mails(self, limit: int | None = None) -> list[MailRecord]:
        async with self._lock:
            conn = self._check_conn()
            rows = conn.execute(
                "SELECT record_json FROM mails ORDER BY received_ts DESC"
                + (" LIMIT ?" if limit is not None else ""),
                (limit,) if limit is not None else (),
            ).fetchall()
        return [MailRecord.model_validate_json(row[0]) for row in rows]

    async def count_mails(self) -> int:
        async with self._lock:
            conn = self._check_conn()
            row = conn.execute("SELECT COUNT(*) FROM mails").fetchone()
        return int(row[0]) if row else 0

    async def set_manual_urgency(
        self, record_id: str, urgency: Urgency | None
    ) -> MailRecord | None:
        async with self._lock:
            conn = self._check_conn()
            row = conn.execute(
                "SELECT record_json FROM mails WHERE record_id = ?", (record_id,)
            ).fetchone()
            if row is None:
                return None
            record = MailRecord.model_validate_json(row[0])
            record.manual_urgency = urgency
            stored = self._without_payload(record)
            conn.execute(
                "UPDATE mails SET record_json = ? WHERE record_id = ?",
                (stored.model_dump_json(), record_id),
            )
            conn.commit()
        return record

    async def delete_mail(self, record_id: str) -> None:
        """Move the full record to trash, stamping the deletion time."""
        async with self._lock:
            conn = self._check_conn()
            row = conn.execute(
                "SELECT record_json FROM mails WHERE record_id = ?", (record_id,)
            ).fetchone()
            if row is None:
                return
            deleted_at = datetime.now(UTC)
            # re-trash cycles (restore -> edit -> delete again) must keep the
            # FIRST deletion timestamp but refresh the stored content, or the
            # trash would restore a stale record
            conn.execute(
                "INSERT INTO trash_records (record_id, record_json, deleted_ts) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(record_id) DO UPDATE SET record_json = excluded.record_json",
                (record_id, row[0], deleted_at.timestamp()),
            )
            conn.execute("DELETE FROM mails WHERE record_id = ?", (record_id,))
            conn.commit()

    async def cleanup_mail(self, before: datetime) -> int:
        """Move active mail received before ``before`` into the trash."""
        cutoff = before.timestamp()
        async with self._lock:
            conn = self._check_conn()
            rows = conn.execute(
                "SELECT record_id, record_json FROM mails WHERE received_ts < ?", (cutoff,)
            ).fetchall()
            deleted_at = datetime.now(UTC)
            moved = 0
            for record_id, record_json in rows:
                conn.execute(
                    "INSERT INTO trash_records (record_id, record_json, deleted_ts) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(record_id) DO UPDATE SET record_json = excluded.record_json",
                    (record_id, record_json, deleted_at.timestamp()),
                )
                conn.execute("DELETE FROM mails WHERE record_id = ?", (record_id,))
                moved += 1
            conn.commit()
        return moved

    # -- trash -----------------------------------------------------------------------------------

    async def list_trash(self) -> list[TrashRecord]:
        async with self._lock:
            conn = self._check_conn()
            rows = conn.execute(
                "SELECT record_json, deleted_ts FROM trash_records ORDER BY deleted_ts DESC"
            ).fetchall()
        trash: list[TrashRecord] = []
        for record_json, deleted_ts in rows:
            record = MailRecord.model_validate_json(record_json)
            deleted_at = datetime.fromtimestamp(float(deleted_ts), tz=UTC)
            trash.append(
                TrashRecord(
                    record_id=record.record_id,
                    mail=record.mail,
                    auto_urgency=record.auto_urgency,
                    manual_urgency=record.manual_urgency,
                    analysis=record.analysis,
                    processor_notes=record.processor_notes,
                    deleted_at=deleted_at,
                    expires_at=deleted_at,
                )
            )
        return trash

    async def restore_from_trash(self, record_id: str) -> MailRecord | None:
        async with self._lock:
            conn = self._check_conn()
            row = conn.execute(
                "SELECT record_json FROM trash_records WHERE record_id = ?", (record_id,)
            ).fetchone()
            if row is None:
                return None
            record = MailRecord.model_validate_json(row[0])
            conn.execute("DELETE FROM trash_records WHERE record_id = ?", (record_id,))
            stored = self._without_payload(record)
            conn.execute(
                "INSERT OR REPLACE INTO mails (record_id, record_json, received_ts) VALUES (?, ?, ?)",
                (record.record_id, stored.model_dump_json(), record.received_at.timestamp()),
            )
            conn.commit()
        return record

    async def purge_trash(self, before: datetime) -> int:
        """Permanently delete trash whose *deletion time* predates ``before``."""
        cutoff = before.timestamp()
        async with self._lock:
            conn = self._check_conn()
            cursor = conn.execute("DELETE FROM trash_records WHERE deleted_ts < ?", (cutoff,))
            conn.commit()
        return int(cursor.rowcount)

    # -- drafts ---------------------------------------------------------------------------------------

    async def save_draft(self, draft: ReplyDraft) -> None:
        async with self._lock:
            conn = self._check_conn()
            conn.execute(
                "INSERT OR REPLACE INTO drafts (draft_id, draft_json) VALUES (?, ?)",
                (draft.draft_id, draft.model_dump_json()),
            )
            conn.commit()

    async def get_draft(self, draft_id: str) -> ReplyDraft | None:
        async with self._lock:
            conn = self._check_conn()
            row = conn.execute(
                "SELECT draft_json FROM drafts WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        return ReplyDraft.model_validate_json(row[0]) if row else None

    async def delete_draft(self, draft_id: str) -> None:
        async with self._lock:
            conn = self._check_conn()
            conn.execute("DELETE FROM drafts WHERE draft_id = ?", (draft_id,))
            conn.commit()

    # -- preferences ----------------------------------------------------------------------------------

    async def get_preference(self, key: str) -> str | None:
        async with self._lock:
            conn = self._check_conn()
            row = conn.execute("SELECT value FROM preferences WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    async def set_preference(self, key: str, value: str) -> None:
        async with self._lock:
            conn = self._check_conn()
            conn.execute(
                "INSERT OR REPLACE INTO preferences (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()

    # -- custom action items ---------------------------------------------------

    async def save_custom_action(self, item: ActionItem) -> None:
        async with self._lock:
            conn = self._check_conn()
            conn.execute(
                "INSERT OR REPLACE INTO custom_actions (item_id, item_json) VALUES (?, ?)",
                (item.item_id, item.model_dump_json()),
            )
            conn.commit()

    async def list_custom_actions(self) -> list[ActionItem]:
        async with self._lock:
            conn = self._check_conn()
            rows = conn.execute("SELECT item_json FROM custom_actions ORDER BY item_id").fetchall()
        return [ActionItem.model_validate_json(row[0]) for row in rows]

    async def delete_custom_action(self, item_id: str) -> bool:
        async with self._lock:
            conn = self._check_conn()
            cursor = conn.execute("DELETE FROM custom_actions WHERE item_id = ?", (item_id,))
            conn.commit()
        return cursor.rowcount > 0


PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-storage-sqlite",
    name="SQLite Storage",
    version="0.1.0",
    description="Durable sqlite persistence with a seven-day recovery trash",
    kinds=[ComponentKind.STORAGE],
)


class StoragePlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        registrar.add_storage("sqlite", SQLiteStorage)


plugin = StoragePlugin()

__all__ = ["SQLiteStorage", "StoragePlugin", "plugin"]
