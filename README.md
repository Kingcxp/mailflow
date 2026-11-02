# MailFlow

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
| Mail | Multiple accounts, historical browsing, body/attachment-name/OCR search, manual urgency and reanalysis |
| Batch actions | Check rows, Shift-select a range, Ctrl+A for visible results, delete expired mail and undo the last deletion |
| Tasks | Mail obligations, custom tasks, seminar review, reminders and summaries |
| Replies | Continue saved drafts; prepare and confirm before sending; failed sends remain retryable |
| Notifications | Configure chat-platform instances and notification targets; export NoneBot or AstrBot plugins |
| Plugins | Install, enable or disable mail sources, model backends, processors, notifiers, storage and gateways |
| Language | English and Simplified Chinese; additional JSON language packs |

Mail deletion moves records to recoverable trash. Defaults are 30-day mail retention,
7-day trash retention and cleanup at 04:00 in the configured timezone. Automatic updates
are disabled by default.

Smart instructions propose changes for confirmation. Search evaluates mail excerpts,
so long messages can contain relevant text outside the excerpt. Remote TUI supports
mail, tasks and reply workflows; smart instructions, conversational correction and
local plugin/process management are currently local-only.

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
