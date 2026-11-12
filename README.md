# MailFlow


Saved filters, account-isolated conversations, and full mail composition (To/Cc/Bcc, forwarding, reply-all and managed attachments) are available from the mail tools menu and shared chat/CLI commands. Chat uploads stay with their sender and session.

[简体中文](README.zh-CN.md)

MailFlow brings multiple mail accounts into one inbox. It combines local storage,
optional model analysis, timed tasks and reminders, confirmed replies, and chat notifications.
Use the terminal interface, command shell, or authenticated remote service.

## Start

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/) and Git.

```bash
git clone https://github.com/Kingcxp/mailflow.git
cd mailflow
uv sync --locked --all-packages --group dev
cp configs/example.toml configs/local.toml
uv run mailflow tui -c configs/local.toml
```

Add accounts and models in the interface or edit the copied TOML. Use environment
variables for credentials; `configs/local.toml` is ignored by Git. Models are optional:
without one, deterministic rules remain available.

## What you can do

| Area | Available behavior |
|---|---|
| Mail | Read originals before background analysis; account/date/sender filters, read/star/archive states, full text, attachments and OCR search |
| Batch actions | Check rows, Shift-select a range, Ctrl+A for visible results, delete expired mail and undo the last deletion |
| Tasks | Optional deadlines, overdue items, categories/tags, recurring plans, pause/skip, mute or snooze reminders; completion removes the occurrence |
| Replies | Search drafts and sent replies; prepare and confirm before sending; failed sends remain retryable |
| Notifications | Configure chat-platform instances and notification targets; export NoneBot or AstrBot plugins |
| Plugins | Install, enable or disable mail sources, model backends, processors, notifiers, storage and gateways |
| Language | English and Simplified Chinese; additional JSON language packs |

Deletion moves mail to MailFlow trash and leaves the source mailbox unchanged. Restore,
search and permanent deletion are available in Mail → More. Mail is kept indefinitely by
default; automatic cleanup is opt-in and protects starred, important and unfinished-task
mail. Trash is kept for 7 days. Backup and restore are under Settings. Automatic updates
are disabled by default; existing retention settings are preserved.

The independent Assistant tab finds or deletes mail, manages tasks and shows live progress. Changes
require confirmation and can be excluded individually. Select the scope from Mail → More.
Long messages are checked in chunks; incomplete searches are reported. Remote TUI supports
mail, tasks, replies, smart query and conversational correction. Plugin/process management
and backup/restore run locally.

## Other entry points

```bash
uv run mailflow shell -c configs/local.toml
uv run mailflow command "help" -c configs/local.toml
uv run mailflow serve -c configs/local.toml
uv run mailflow tui --remote http://127.0.0.1:8800 -c configs/local.toml
```

Configure server credentials before remote use. The command help is the reference for
available operations and arguments; [configuration](docs/configuration/overview.md)
covers accounts, models, OCR, gateways and remote access.

## Documentation

| Need | Read |
|---|---|
| Configure or operate MailFlow | [Configuration](docs/configuration/overview.md) |
| Develop, test or build | [Development](docs/development/setup.md) |
| Understand data and processing | [Architecture](docs/architecture/overview.md) |
| Embed the service | [Embedding](docs/development/embedding.md) |
| Write a plugin | [Plugin contracts](docs/plugin-development/overview.md) |
| Browse or publish plugins | [Plugin repository](https://github.com/Kingcxp/mailflow-repo) |
| Release changes | [Changelog](CHANGELOG.md) |

Run `make help` for development commands. Core is independent of UI and provider
packages; bundled plugins live in `plugins/`, hosts in `packages/`, and example
configuration and translations in `configs/` and `translations/`.

License: MIT.
