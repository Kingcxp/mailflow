# Storage backend

A storage backend persists records, trash, drafts and preferences.

## Contract

See `mailflow/contracts.py` — `StorageBackend` (async methods):

- lifecycle: `initialize()`, `close()`
- active mail: `save_mail`, `get_mail`, `list_mails(limit)`, `count_mails`,
  `set_manual_urgency(record_id, urgency|None)` (returns the updated record),
  `delete_mail` (moves to trash)
- trash: `list_trash`, `restore_from_trash`, `purge_trash(before) -> int`,
  `cleanup_mail(before) -> int`
- drafts: `save_draft`, `get_draft`, `delete_draft`
- preferences: `get_preference(key)`, `set_preference(key, value)`

## Registration

```python
def build_storage(config: StorageConfig) -> MyStorage:
    return MyStorage(config.path, config.options)


registrar.add_storage("my-storage", build_storage)
```

## Semantics that must hold

- **Trash carries the full record.** `delete_mail`/`cleanup_mail` move the
  complete serialized record (analysis, processor notes, manual urgency) to
  the trash with a deletion timestamp.
- **Purge compares the deletion timestamp**, never the mail receipt time.
- **Restore returns the identical record**, with the original `received_at`.
- **First-deletion time is preserved**: if the same record is cleaned again
  after a re-sync, keep the original trash timestamp (the sqlite backend
  uses `INSERT OR IGNORE`).
- **Manual urgency by reserialization**: load the record, set
  `manual_urgency`, persist the full record again — never a partial update
  that drops other fields.
- Prefer parameterized SQL; guard one connection with an async lock in WAL
  mode (see `mailflow-storage-sqlite`).

## Preferences

`preferences` is where the persistent language lives (key `language`). Hosts
read/write through the service, so no direct access is needed.
