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

## Clearing everything (manual reset)

Two explicit, separate steps, exposed as chat commands and make targets:

| Command | Make target | Effect |
| ------- | ----------- | ------ |
| `clean records` | `make clean-records` | `service.clear_all_records()`: every stored mail moves to the **trash**, every schedule entry is removed. Recoverable — the trash is left intact. |
| `clean trash CONFIRM` | `make clean-trash` | `service.purge_trash_now()`: permanently deletes the entire trash. Irreversible. |

The split is deliberate: the recoverable half can never destroy what it just
moved. `clean records` is a reset, not a partial sweep — it also drops the
derived state that described the cleared records (the `actions.dismissed`
dismissal list and the stored `seminars.candidates` proposals), because a
surviving dismissal would keep hiding an entry for a mail nobody can see any
more. The in-memory dedup state is reset too, so a later re-sync can process
the same mail again.

Both are also reachable from the service API (`clear_all_records`,
`purge_trash_now`) and emit `mailflow.records.cleared` /
`mailflow.trash.purged`. The destructive half refuses to run without the
literal word `CONFIRM` (a bare `clean trash` explains and exits non-zero).
