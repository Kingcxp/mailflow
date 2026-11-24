<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/wordmark-dark.svg">
    <img src="assets/wordmark-light.svg" alt="Waymark Mail" width="660">
  </picture>
</p>

<p align="center">
  <a href="README.zh-CN.md">简体中文</a> ·
  <a href="docs/configuration/overview.md">User guide</a> ·
  <a href="https://github.com/Kingcxp/waymark-mail-repo/tree/dev">Plugin repository</a> ·
  <a href="https://github.com/Kingcxp/waymark-mail/issues">Issues</a>
</p>

<p align="center">
  <a href="https://github.com/Kingcxp/waymark-mail/actions/workflows/ci.yml?query=branch%3Adev"><img src="https://github.com/Kingcxp/waymark-mail/actions/workflows/ci.yml/badge.svg?branch=dev" alt="CI"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.11%2B-4b72bd" alt="Python 3.11+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-147d83" alt="MIT License"></a>
</p>

Waymark Mail is a terminal email client with tasks and bot notifications. It brings
multiple accounts into a local inbox, where you can read, search, reply and keep
track of what needs doing.

Connect a model for summaries and natural-language requests such as “find this
week’s invoices” or “make a task from this email”. Changes require your
confirmation. Reading, ordinary search and manual operations work without a model.

## Features

- **Mail:** saved filters, conversations, read status, stars, archives and a local trash.
- **Writing:** compose, reply, reply all and forward, with drafts, To/Cc/Bcc and attachments.
- **Tasks:** categories, tags, optional deadlines and daily, weekly or monthly plans. Postpone an occurrence or pause the plan.
- **Notifications:** use the built-in Console or connect a chat platform. Choose notification presets, quiet hours and delivery targets.
- **Files and backups:** find attachments across mailboxes, download in batches and back up configuration and local data.

The interface supports English and Simplified Chinese. Summaries follow your
selected language. Color schemes include VS Code Dark, VS Code Light and the four
Catppuccin palettes; more can be installed as plugins.

## Quickstart

These instructions use the **`dev` branch**. You need Python 3.11+, Git and
[uv](https://docs.astral.sh/uv/).

```bash
git clone --branch dev https://github.com/Kingcxp/waymark-mail.git
cd waymark-mail
uv sync --locked --all-packages --group dev
```

Copy the example configuration once:

| Shell | Command |
| --- | --- |
| Linux / macOS | `cp configs/example.toml configs/local.toml` |
| Windows PowerShell | `Copy-Item configs/example.toml configs/local.toml` |

Then start the terminal interface:

```bash
uv run --no-sync waymark tui -c configs/local.toml
```

Open **Mail & models** to add an account. Choose whether to receive only new
mail or import history, then test receiving and sending. A model and external
notification channels can be added later. To fetch messages missed while the
service was stopped, enable the account’s catch-up option.

Enter a request beside the mail or task list and choose **Smart action**. The
**Console** tab also accepts commands: try `/waymark help`, or address the
assistant with `@Waymark`.

Keep `--no-sync` on everyday commands to preserve installed plugins. Run
`make sync` when you want to synchronize dependencies; `make help` only shows help.

## Running in the background

**F8** detaches the terminal and keeps the instance running. **Ctrl+Q** stops it.
You can also start, find and reconnect to a background instance:

```bash
uv run --no-sync waymark tui -c configs/local.toml --background --name inbox
uv run --no-sync waymark tui list
uv run --no-sync waymark tui attach inbox
uv run --no-sync waymark tui stop inbox
```

For a headless service, use `waymark run -c configs/local.toml`. For remote access,
configure server credentials, start `waymark serve -c configs/local.toml`, then
connect with `waymark tui --remote http://127.0.0.1:8800`. See
[remote access](docs/configuration/overview.md#remote-access) for configuration.

Press **F1** for terminal shortcuts, or run `waymark --help` for CLI commands.
Single commands do not start background mail, reminder or backup loops.

## Data and privacy

Deleting mail moves it to the local trash, which keeps messages for 7 days by
default. Changes to the source mailbox are opt-in per account. Completing a task
permanently removes that occurrence; a recurring plan continues.

When you enable a model or notification channel, the content needed for that
operation is sent to the configured provider. Plugins run code in the host
environment. Configuration files, backups and exported bot packages may contain
credentials; keep them private.

See [backup and cleanup](docs/configuration/overview.md#data-backup-and-cleanup)
for retention settings, scheduled backups and restore instructions.

## Upgrading from MailFlow

Existing configuration, SQLite data, credentials and plugins remain usable.
`mailflow` and `/mailflow` commands still work; new examples use `waymark`.
Python imports and plugin IDs keep their existing `mailflow` names.

Update an existing checkout and add the new launcher without replacing its plugins:

```bash
git remote set-url origin https://github.com/Kingcxp/waymark-mail.git
uv pip install --no-deps -e packages/waymark-mail
```

To import another installation, open **Settings → More → Import old settings**
and select its TOML file. Review the preview before confirming; including its
SQLite database is optional. Import creates a recovery backup first.

The plugin repository is now **[waymark-mail-repo](https://github.com/Kingcxp/waymark-mail-repo/tree/dev)**.
Saved addresses using either previous name are recognized automatically.

## Documentation and contributing

| Guide | Contents |
| --- | --- |
| [User guide](docs/configuration/overview.md) | Accounts, models, notifications, remote access and troubleshooting |
| [Development](docs/development/setup.md) | Setup, checks, contributing and releases |
| [Architecture](docs/architecture/overview.md) | Processing, shared models and service lifecycle |
| [Embedding](docs/development/embedding.md) | Using the service in another application |
| [Plugin development](docs/plugin-development/overview.md) | Interfaces, capabilities and offline validation |
| [Changelog](CHANGELOG.md) | Changes awaiting release |

Bug reports and contributions are welcome. Open an
[issue](https://github.com/Kingcxp/waymark-mail/issues) with your revision, OS,
terminal and steps to reproduce. Remove credentials and private mail from any
logs. Pull requests target `dev`.

Licensed under the [MIT License](LICENSE).
