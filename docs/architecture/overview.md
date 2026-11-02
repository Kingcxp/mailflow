# Architecture

MailFlow is one mail service with several hosts. The CLI, TUI, REST/WebSocket
server and exported bot plugins share the same configuration, plugin registry,
service methods and permission rules.

## Code map

| Location | Responsibility |
| --- | --- |
| `packages/mailflow-core` | Domain models, contracts, configuration, processing, scheduling, commands and service lifecycle |
| `packages/mailflow-bundled` | Explicit registration of the plugins shipped with the application |
| `packages/mailflow-cli` | Command-line entry points |
| `packages/mailflow-tui` | Local and remote terminal interface |
| `packages/mailflow-server` | Authenticated REST/WebSocket transport and remote adapter |
| `packages/mailflow-testkit` | Reusable fakes and offline plugin-contract checks |
| `plugins` | Bundled mail, model, notification, storage and framework-export components |
| [Plugin repository](https://github.com/Kingcxp/mailflow-repo) | Marketplace metadata, additional plugins and their usage instructions |

## Mail processing

An enabled source emits a normalized `MailMessage` into the runtime queue. Workers
apply the configured processor chain, merge analysis, persist a `MailRecord`
and publish events and eligible notifications. Without a usable model, rule
processing remains available.

Mail identity includes its source/account scope: identical Message-ID values
from different accounts remain separate. Retrying the same mail is idempotent.
A source advances its live cursor only after the runtime accepts the mail;
browsing history does not advance that cursor. A failed batch must leave unread
or unaccepted items available for retry.

Attachment bytes remain available during extraction and optional OCR. SQLite
stores attachment metadata and extracted `image_text`, not attachment binaries.
Search includes subjects, senders, bodies, summaries, filenames and OCR text.
Smart search uses excerpts, so a detail outside those excerpts can be missed.

Reanalysis preserves user feedback, manual urgency and local task state.
Failure preserves the last valid analysis; retrying does not create duplicate
mail-derived tasks.

## Tasks, reminders and replies

Mail-derived, custom and seminar tasks retain their own storage/deletion rules.
Their shared effective view drives lists, search, reminders, digests and cleanup,
so a deleted task cannot reappear in another view. Due dates and reminder
windows use the configured timezone. Delivery receipts are saved after a
notification succeeds; failed targets remain retryable.

Reply drafts retain their content across preparation and send failures. Sending
requires a valid account/source and an explicit confirmation token. Per-draft
locking prevents simultaneous or repeated confirmation from sending twice;
only a successful send becomes sent.

## Hosts, permissions and lifecycle

[MailFlowService](../../packages/mailflow-core/src/mailflow/service.py) owns
storage, runtime, gateways and bridges. Reload stops old components before
replacing them. Shutdown cancels source tasks, workers and scheduled loops,
stops owned gateways/bridges and closes storage. Hosts must await shutdown on
the event loop that started the service.

[Shared permissions](../../packages/mailflow-core/src/mailflow/permissions.py)
carry sender, provider, instance and chat identity. Ordinary users may inspect
mail, status and help. Mutating mail/tasks, sending replies, changing settings,
plugins, updates and gateways requires administrator privileges. Every external
transport supplies an explicit context; Basic Auth does not replace the shared
checks.

The remote adapter converts JSON dates, enums and nested records into the same
models used by the TUI. Remote capabilities are explicit: unsupported local
operations are hidden or return a translated error. Model text never directly
executes a write; smart actions stage a plan for host confirmation.

## Where to make changes

Use [configuration](../configuration/overview.md) for settings and defaults,
[embedding](../development/embedding.md) for host integration,
[plugin development](../plugin-development/overview.md) for component contracts,
and [development](../development/setup.md) for tests, checks and packaging.
