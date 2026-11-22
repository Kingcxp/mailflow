"""Release data formats. Bump these only for a released format change."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from mailflow.domain import MailRecord, to_utc
from mailflow.ocr import searchable_record_text

CONFIG_VERSION = 1
DATABASE_VERSION = 1


class DataVersionError(ValueError):
    def __init__(self, kind: str, found: int, supported: int) -> None:
        super().__init__(
            f"This {kind} uses data version {found}; this MailFlow supports version {supported}. "
            "Use a newer MailFlow to open it. The original file has not been changed."
        )
        self.code = f"migration.{kind}_newer"
        self.params = {"found": found, "supported": supported}


def check_version(value: object, kind: Literal["config", "database"]) -> int:
    supported = CONFIG_VERSION if kind == "config" else DATABASE_VERSION
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"Invalid {kind} data version")
    if value > supported:
        raise DataVersionError(kind, value, supported)
    return value


def _config_v1(raw: dict[str, Any]) -> None:
    # Freeze the first release's legacy defaults, independently of future defaults.
    for group, previous in (("llms", 60.0), ("processors", 30.0)):
        entries = raw.get(group, [])
        if not isinstance(entries, list):
            continue  # Let the normal configuration parser explain invalid input.
        for value in cast(list[Any], entries):
            if isinstance(value, dict):
                entry = cast(dict[str, Any], value)
                if entry.get("timeout_seconds") == previous:
                    entry["timeout_seconds"] = 120.0


_CONFIG_STEPS: dict[int, Callable[[dict[str, Any]], None]] = {1: _config_v1}


def migrate_config(raw: dict[str, Any]) -> dict[str, Any]:
    previous = check_version(raw.get("schema_version", 0), "config")
    updated = deepcopy(raw)
    for version in range(previous + 1, CONFIG_VERSION + 1):
        _CONFIG_STEPS[version](updated)
        updated["schema_version"] = version
    return updated


def recovery_path(path: Path, version: int) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return path.with_name(f"{path.name}.before-v{version}-{stamp}-{uuid4().hex[:6]}.bak")


def backup_database(conn: sqlite3.Connection, path: Path) -> None:
    """A consistent copy includes committed WAL data without changing the source."""
    try:
        snapshot = sqlite3.connect(path)
        try:
            path.chmod(0o600)
            conn.backup(snapshot)
            snapshot.execute("PRAGMA journal_mode=DELETE")
        finally:
            snapshot.close()
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def read_database(path: Path) -> bytes:
    """Read an existing SQLite file, including its WAL, without opening a writer."""
    with tempfile.TemporaryDirectory(prefix="mailflow-import-") as folder:
        source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            check_version(source.execute("PRAGMA user_version").fetchone()[0], "database")
            destination = Path(folder) / "snapshot.db"
            backup_database(source, destination)
            return destination.read_bytes()
        finally:
            source.close()


def legacy_database_path(config_path: Path, raw: dict[str, Any]) -> Path:
    # A remote client only needs the data path; server-only credentials are
    # expanded and validated by the destination service, never by the picker.
    check_version(raw.get("schema_version", 0), "config")
    storage = raw.get("storage", {})
    if not isinstance(storage, dict):
        raise ValueError("Invalid storage configuration")
    configured = cast(dict[str, Any], storage).get("path", "data/mailflow.db")
    if not isinstance(configured, str):
        raise ValueError("Invalid database path")
    if configured.startswith("${") and configured.endswith("}"):
        configured = os.environ.get(configured[2:-1], configured)
    path = Path(configured).expanduser()
    if path.is_absolute():
        return path
    roots = [config_path.parent]
    if config_path.parent.name.casefold() in {"configs", "config"}:
        roots.insert(0, config_path.parent.parent)
    candidates = [root / path for root in roots]
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


_SCHEMA = """
CREATE TABLE IF NOT EXISTS mails (
    record_id TEXT PRIMARY KEY, record_json TEXT NOT NULL,
    received_ts REAL NOT NULL, search_text TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS trash_records (
    record_id TEXT PRIMARY KEY, record_json TEXT NOT NULL, deleted_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (draft_id TEXT PRIMARY KEY, draft_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS custom_actions (item_id TEXT PRIMARY KEY, item_json TEXT NOT NULL);
"""


def _database_v1(conn: sqlite3.Connection) -> None:
    for statement in _SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(mails)")}
    build_search = "search_text" not in columns
    if build_search:
        conn.execute("ALTER TABLE mails ADD COLUMN search_text TEXT NOT NULL DEFAULT ''")
    rebuild_dates = conn.execute(
        "SELECT value FROM preferences WHERE key = 'schema.mail_received_index'"
    ).fetchone() != ("1",)
    if build_search or rebuild_dates:
        rows = conn.execute("SELECT record_id, record_json FROM mails")
        while batch := rows.fetchmany(200):
            for key, raw in batch:
                record = MailRecord.model_validate_json(raw)
                if build_search:
                    conn.execute(
                        "UPDATE mails SET search_text = ? WHERE record_id = ?",
                        (searchable_record_text(record).casefold(), key),
                    )
                if rebuild_dates:
                    conn.execute(
                        "UPDATE mails SET received_ts = ? WHERE record_id = ?",
                        (to_utc(record.mail.received_at).timestamp(), key),
                    )
        conn.execute("INSERT OR REPLACE INTO preferences VALUES('schema.mail_received_index', '1')")
    conn.execute("CREATE INDEX IF NOT EXISTS mails_received ON mails(received_ts DESC, record_id)")


_DATABASE_STEPS: dict[int, Callable[[sqlite3.Connection], None]] = {1: _database_v1}


def migrate_database(conn: sqlite3.Connection) -> int:
    """Apply all required release steps in one transaction, retaining original IDs."""
    previous = check_version(conn.execute("PRAGMA user_version").fetchone()[0], "database")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables and "mails" not in tables:
        raise ValueError("Not a MailFlow database")
    if previous == DATABASE_VERSION:
        return previous
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        for version in range(previous + 1, DATABASE_VERSION + 1):
            _DATABASE_STEPS[version](conn)
            conn.execute(f"PRAGMA user_version={version}")
    return previous
