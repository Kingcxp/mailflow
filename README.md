# MailFlow

**A terminal inbox for mail, follow-up tasks and an optional AI assistant.**

[简体中文](README.zh-CN.md) · [Configuration](docs/configuration/overview.md) ·
[Contributing](docs/development/setup.md#contributing) ·
[Issues](https://github.com/Kingcxp/mailflow/issues)

[![CI](https://github.com/Kingcxp/mailflow/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/Kingcxp/mailflow/actions/workflows/ci.yml?query=branch%3Adev)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)

MailFlow collects multiple accounts in a locally stored inbox. Read and organize
mail, write replies, track recurring tasks and receive notifications through your
existing chat platforms. Use the terminal interface, shared commands or a remote
terminal connected to your own server. Models are optional: reading, ordinary
search and rule processing work without one.

## Features

| Area | What you can do |
| --- | --- |
| Mail | Read full messages before analysis finishes; filter by account and date, save rolling filters, view conversations, star and archive mail |
| Search and assistant | Search text, attachment names and extracted image text; use a separate assistant tab for natural-language requests, progress and confirmed changes |
| Writing | Compose, reply, reply all or forward with To/Cc/Bcc and attachments; keep drafts, preview and confirm before sending |
| Tasks | Use optional deadlines, categories, tags and daily/weekly/monthly plans; postpone one occurrence, pause a plan or snooze its reminders |
| Notifications | Connect supported bots or delivery plugins; choose presets, quiet hours and targets; inspect and retry failed deliveries |
| Files and recovery | Find and download attachments, exchange files on supported chat channels, restore trash and create portable or scheduled backups |
| Access and extensions | Use local or remote TUI, CLI and existing bot integrations; extend sources, models, processing, delivery and storage through plugins |

English and Simplified Chinese are built in. Generated summaries follow the
selected interface language; older analysis in another language can be reanalyzed.

## Quickstart

These instructions match the **`dev` branch**. Use Python 3.11+, Git and
[uv](https://docs.astral.sh/uv/). Windows and Linux are covered by CI.

```bash
git clone --branch dev https://github.com/Kingcxp/mailflow.git
cd mailflow
uv sync --locked --all-packages --group dev
```

Copy the example configuration once, then launch MailFlow:

```bash
# Linux / macOS
cp configs/example.toml configs/local.toml
```

```powershell
# Windows PowerShell
Copy-Item configs/example.toml configs/local.toml
```

```bash
uv run mailflow config-check -c configs/local.toml
uv run mailflow tui -c configs/local.toml
```

In the interface, add an account, choose whether to receive only new mail or
import history, and check receiving and sending separately. Add a model if you
want summaries or the assistant; connect a notifier only if you want external
delivery. The example has no account or model credentials. Keep your local
configuration private and use environment variables for secrets.

To receive messages missed while MailFlow was stopped, enable the account's
catch-up option. It uses a saved source cursor; it does not silently import the
entire mailbox. See [account setup](docs/configuration/overview.md#accounts-and-startup).

## Running modes

| Need | Command |
| --- | --- |
| Terminal interface | `uv run mailflow tui -c configs/local.toml` |
| Background terminal instance | `uv run mailflow tui -c configs/local.toml --background --name inbox` |
| List / reconnect | `uv run mailflow tui list` / `uv run mailflow tui attach inbox` |
| Foreground service | `uv run mailflow run -c configs/local.toml` |
| Authenticated server | `uv run mailflow serve -c configs/local.toml` |
| Remote terminal | `uv run mailflow tui --remote http://127.0.0.1:8800` |
| Interactive commands | `uv run mailflow shell -c configs/local.toml` |
| One command | `uv run mailflow command "help" -c configs/local.toml` |

**F8 detaches** a persistent TUI and leaves it running; **Ctrl+Q stops** it.
`mailflow tui stop inbox` stops a background instance. Single commands and the
command shell do not start automatic mail, reminder or backup loops.

Set server credentials before remote use. The remote terminal operates on server
data; files selected for upload come from the client. Local plugin installation
and process management are not remote terminal operations.

## Data and operation rules

| Operation | Behavior |
| --- | --- |
| Delete mail | Moves it to MailFlow trash by default; restore or permanently clear local trash from Mail → More |
| Source mailbox changes | Off by default, enabled per account; supported read/star/archive changes synchronize, source trash/restore requires explicit confirmation, and source mail is never permanently purged |
| Retention | Mail is kept indefinitely by default; trash expires after 7 days; automatic mail cleanup is opt-in and protects important/starred mail and unfinished tasks |
| Complete a task | Removes that occurrence permanently; a recurring plan continues, with no completed-task history |
| Import or reanalyze | Quiet by default; analysis is a cancellable task and is not resumed automatically after application exit |
| Assistant changes | Require confirmation tied to the initiating user/session; history defaults to 30 days, while execution tasks and confirmation tokens are temporary |
| Backups | Include configuration and available local data/files; automatic backups are off by default; bot exports of backups are private-chat only |

With a model enabled, the configured provider receives the content required for
the requested analysis or assistant operation. Chat notifications send selected
content to their configured platforms. Plugins run Python code in the host
environment: install only code you trust. Backups and generated bot packages can
contain credentials and should not be published.

## Documentation and support

| Read | Covers |
| --- | --- |
| [Configuration and operation](docs/configuration/overview.md) | Accounts, language, models, notifications, remote access, backup and troubleshooting |
| [Development and contributing](docs/development/setup.md) | Setup, checks, issue reports, pull requests and packaging |
| [Architecture](docs/architecture/overview.md) | Shared data rules, processing and resource ownership |
| [Service embedding](docs/development/embedding.md) | Using the service in another host |
| [Plugin development](docs/plugin-development/overview.md) | Core contracts, optional capabilities and offline validation |
| [Plugin repository](https://github.com/Kingcxp/mailflow-repo/tree/dev) | Available plugins, per-plugin configuration and marketplace publishing |
| [Changelog](CHANGELOG.md) / [Releases](https://github.com/Kingcxp/mailflow/releases) | Unreleased changes and published artifacts |

Report reproducible problems through [GitHub Issues](https://github.com/Kingcxp/mailflow/issues).
Include the revision, OS, terminal, sanitized configuration and steps to reproduce.
Do not attach credentials, private mail, databases or backups. Automated checks
use simulated services; real platform login, file delivery and terminal behavior
also need manual verification.

## License

MailFlow is licensed under the [MIT License](LICENSE). Third-party services,
libraries and gateway runtimes retain their own licenses and usage requirements.
