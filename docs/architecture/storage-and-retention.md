# Storage and retention

## SQLite backend

`plugins/mailflow-storage-sqlite` persists full domain records as JSON:

- A single connection guarded by an `asyncio.Lock`, WAL mode and a busy
  timeout. Tables: `mails`, `trash_records`, `drafts`, `preferences`.
- Attachment payloads are stripped before persisting (original text/HTML
  stays intact); manual urgency is applied by reserializing the record.
- All mutations are parameterized (no SQL injection surface).

## Trash semantics

- Deletion — manual (`delete_mail`) or retention (`cleanup_mail`) — moves the
  **full** record to `trash_records` stamped with `deleted_at`.
- Restore returns the identical record (`INSERT OR REPLACE` back into
  `mails` with the original `received_at`).
- Purge compares the **trash deletion timestamp**, never the receipt time —
  a mail that was received two months ago but deleted yesterday is still
  recoverable for seven days.
- `INSERT OR IGNORE` preserves the first deletion timestamp when the same
  record re-appears and is cleaned again (the 7-day window is not restarted
  by re-syncs).

## Retention schedule

Defaults in `[general]`: `mail_retention_days = 30`, `trash_retention_days =
7`, cleanup at `04:00` local time (`cleanup_hour`/`cleanup_minute`).

The runtime's cleanup task computes the next 04:00 in the configured
timezone (`ZoneInfo`, with `tzdata` on Windows) and sleeps until then. Each
run:

1. `cleanup_mail(before)` — moves active mail received before
   `now - mail_retention_days` into the trash.
2. `purge_trash(before)` — permanently deletes trash whose deletion time
   predates `now - trash_retention_days`.

Manual `service.run_cleanup()` performs the same work on demand; the
`cleanup.done` event reports counts.
