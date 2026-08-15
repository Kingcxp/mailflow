# Architecture overview

MailFlow is a plugin pipeline for unified mail: multiple provider adapters
merge into one bounded stream, an ordered processor chain classifies each
mail (four-level urgency, summary, reply flag, timed action items), results
are persisted with a recoverable trash, and CLI/TUI/bot hosts render the
system state through one embeddable service facade.

## Layers

```
Hosts (mailflow-cli, mailflow-tui, chat bots)
        │  command router / service facade (snapshots, queries, mutations)
        ▼
mailflow-core          domain · config · contracts · registry · plugins
                       events · llm router · pipeline · logging · i18n
                       runtime · service · commands
        ▲  component factories (ownership stamped at registration)
        │
plugins/*              mail sources · storage · llm backends · processors · notifiers
mailflow-bundled       composition root: the official plugin set
```

Dependency direction is strictly inward: Core never imports concrete
plugins or UI frameworks; hosts never contain business logic.

## Public boundary

Everything a host needs lives on `MailFlowService`:

- `snapshot()` — plugins, components, accounts (status/errors), LLMs,
  processor→LLM/fallback bindings, storage, language, timezone.
- Queries — `list_mails`, `get_mail`, `count_mails`, `list_actions`,
  `list_trash`.
- Mutations — `set_mail_urgency` (manual override / reset), `delete_mail`,
  `restore_mail`, `set_language` (persisted), `run_cleanup`.
- Reply workflow — `create_reply`, `edit_draft`, `prepare_reply` (token),
  `confirm_reply` (validated, double-send safe), `cancel_reply`.
- `commands.execute(line)` — transport-neutral command responses.
- Events — `service.on("mail.processed", handler)`.

`start_service(config, ...)` is the single entry point that composes
configuration, plugins, storage, LLMs, processors, sources, notifiers, events
and logging. `run_service(...)` is the standalone convenience wrapper.

## Lifecycle

`start_service` → load config → configure logging (queue + sinks + redaction)
→ build plugin manager (bundled set + optional discovery) → build registry →
instantiate storage/sources/LLM backends/processors/notifiers → start runtime
(source tasks per account, pipeline workers, cleanup scheduler). `stop()`
sets the stop event, cancels tasks gracefully, closes sources and storage.

## See also

- `domain-and-mail.md` — the data model and urgency contract
- `plugin-system.md` — hooks, registries, ownership
- `pipeline.md` — ordering, retries, failure policy
- `llm.md` — routing, fallback, secrets
- `logging.md` — queue sinks and redaction
- `storage-and-retention.md` — durability and the daily cleanup
- `replies.md` — the confirmed reply state machine
- `tui.md` — the Textual client
