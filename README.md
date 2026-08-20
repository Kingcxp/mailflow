<div align="center">

# MailFlow

**统一多账户邮件收件箱：插件化过滤、LLM 智能分析、富终端界面与可嵌入核心**

*Unified multi-account mail inbox — plugin pipeline, LLM analysis, rich TUI, embeddable core*

[English](README.md) · [简体中文](README.zh-CN.md)

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![uv](https://img.shields.io/badge/uv-workspace-6c33af?logo=astral)](https://docs.astral.sh/uv/)
[![CI](https://github.com/Kingcxp/mailflow/actions/workflows/ci.yml/badge.svg)](https://github.com/Kingcxp/mailflow/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-317%20passed-67C23A)]()
[![Type checking](https://img.shields.io/badge/mypy%2Fpyright-strict-67C23A)]()
[![Linting](https://img.shields.io/badge/ruff-passing-67C23A)]()
[![Status](https://img.shields.io/badge/status-v0.1.0%20baseline-E6A23C)]()

</div>

MailFlow merges mails from multiple accounts and providers into one stream,
classifies every message with a four-level urgency contract, extracts timed
obligations (exams, meetings, errands) into a schedule table with reminders,
stores everything with a recoverable trash, and surfaces it through a
Textual TUI, a colored command shell, or any chat-bot host embedding the
core. Extend it with plugins — mail sources, LLM backends, processors,
notifiers, storage — installed from a plugin marketplace.

## Features

- **Multi-account, multi-adapter** — provider adapters merge into one bounded
  stream; per-account failure isolation.
- **Four-level urgency contract** — `ad #909399` (junk) · `info #67C23A`
  (useful) · `important #E6A23C` (read it) · `urgent #F56C6C` (act now).
  Colors are part of the public contract, reused by CLI, TUI and notifiers.
- **LLM analysis** — OpenAI-compatible chat completions (works with OpenCode
  relays, llama.cpp, vLLM) plus an Anthropic Claude backend; named LLMs
  with ordered fallback; structured summaries, reasons, reply drafts and
  timed action items.
- **Timed-action table + reminders** — exams/meetings/errands with time,
  type, content and preparation notes, drilling into the source mail;
  reminders fire at a configurable fixed time two days before the due date
  and at midnight on the due day.
- **Two-step confirmed replies** — draft → prepare (short-lived token) →
  confirm; double-send safe, editing invalidates the token.
- **Recoverable retention** — configurable mail retention (default 30 days)
  with a daily 04:00 cleanup; deleted mails recoverable for 7 days in trash.
- **Rich logging** — queue-based rich console output, rotating file, JSONL;
  levels, redirects and secret redaction all configurable; never touches the
  host's root logger.
- **i18n** — English (default) and Simplified Chinese built in; other
  languages load as data-only JSON packs; the choice persists.
- **Plugin marketplace** (VS Code style) — search, category filters, markdown
  details, install / uninstall / enable / disable from commands and the TUI;
  built-ins are categorized (mail_source, processor, llm_backend, notifier,
  storage, bot_exporter); disabling a plugin never breaks startup (orphaned
  config entries are skipped with a warning).
- **Bot-framework export** — turn a configured instance into a plugin for
  NoneBot2, AstrBot or any other chatbot framework (`mailflow export
  --framework <id>`, TUI export wizard with folder tree, `make
  bot-plugin-*`); exporters are plugins themselves, so new frameworks are a
  marketplace install, not a core change.
- **VS Code-style settings** — the TUI Settings tab is a searchable editor: a
  sidebar of sections (general, logging, plugins, storage, language, plus one
  per plugin that owns options) and one card per option with its description,
  default, an editor matching its type, Save and Restore-default. Invalid
  input says which option is wrong and why; nothing persists unless the whole
  config re-validates. Secrets that came from `${ENV_VAR}` or `api_key_env`
  are never written back into the file.
- **Mailboxes and LLMs tabs** — add, edit and delete accounts and LLMs from
  forms. The LLM list order *is* the routing policy: first entry is the
  default, each entry falls back to the ones below it.
- **Browse and analyze existing mail** — new users can page through mail that
  arrived before MailFlow was configured and analyze only the messages they
  pick; picked mails take the same pipeline path as live mail, and
  already-analyzed mail is skipped.
- **Quality gates** — 317 unit/integration/e2e tests, mypy & pyright strict,
  ruff lint + format, Nuitka standalone/onefile executables, docs gate.

## Install

Requires **Python ≥ 3.11** and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/mailflow/mailflow.git
cd mailflow
uv sync --all-packages --group dev
```

## Quick start

```bash
# edit configs/development.toml: add your mailbox(es) and LLM endpoint(s)
uv run mailflow tui -c configs/development.toml
uv run mailflow shell -c configs/development.toml

# or copy the example config, fill in your tokens, and run
cp configs/example.toml configs/local.toml
export YOUR_TOKEN=your-token
uv run mailflow run -c configs/local.toml
```

See [docs/development/setup.md](docs/development/setup.md) for details.

## Commands

```
help                       colored command documentation
mail list|show|delete|urgency <id> <level|auto>
action list|show|add|delete    timed tasks; add "<summary>" --due "YYYY-MM-DD HH:MM" [--type] [--notes]
plugin list|show           plugins, adapters, accounts, llms, bindings
plugin repo add|list|remove    manage marketplaces
plugin market list|show|search <query>   browse/store with markdown details
plugin install|uninstall <id>  install or remove a plugin
plugin enable|disable <id>     toggle a plugin (applies on next start)
export --framework <id> --output <dir>   generate a chatbot-framework plugin (NoneBot, AstrBot, ...)
reply create|compose cn/en|edit|prepare|confirm|cancel   compose: letter template (auto date, right-aligned signature)
lang get|set <code>        switch language (persisted)
trash list|restore         recover deleted mail
config list|get|set        inspect and change every option
```

## Embedding in a bot or service

```python
from mailflow.service import start_service
from mailflow_bundled import create_plugin_manager

service = await start_service(
    config,
    plugin_manager=create_plugin_manager(config),
    extra_log_handlers=[my_host_handler],
)
snapshot = service.snapshot()  # plugins, accounts, LLMs, bindings
mails = await service.list_mails()  # full records + analysis + actions
service.on("mailflow.mail.processed", handler)  # async events
await service.commands.execute("mail list")
await service.stop()
```

See [docs/development/embedding.md](docs/development/embedding.md).

## Urgency contract

| Level       | Color    | Meaning                                             |
| ----------- | -------- | --------------------------------------------------- |
| `ad`        | #909399  | irrelevant advertising / junk                       |
| `info`      | #67C23A  | useful but not time-critical (lecture notice)       |
| `important` | #E6A23C  | needs reading (verification code)                   |
| `urgent`    | #F56C6C  | must be handled now or at a specific time (exam)    |

Manual overrides win while set; reset restores the automatic value.

## Quality gates

```bash
make help           # grouped, colored list of every target
make check          # lint + format + mypy + pyright + pytest + docs gate
make coverage       # per-package coverage report
make build          # wheels for every package
make bot-plugin-nonebot | bot-plugin-astrbot   # export the NoneBot / AstrBot plugin
make bot-plugin FRAMEWORK=<id> OUTPUT=<dir>     # export for any installed exporter
make exe-standalone # Nuitka standalone (smoke test before onefile)
make exe-onefile
```

## Documentation

| Area | Links |
| ---- | ----- |
| Architecture | [overview](docs/architecture/overview.md) · [domain & mail](docs/architecture/domain-and-mail.md) · [plugins](docs/architecture/plugin-system.md) · [pipeline](docs/architecture/pipeline.md) · [LLM](docs/architecture/llm.md) · [logging](docs/architecture/logging.md) · [storage & retention](docs/architecture/storage-and-retention.md) · [replies](docs/architecture/replies.md) · [TUI](docs/architecture/tui.md) · [bot export](docs/architecture/bot-export.md) |
| Development | [setup](docs/development/setup.md) · [deployment](docs/development/deployment.md) · [embedding](docs/development/embedding.md) · [tests](docs/development/tests.md) · [quality](docs/development/quality.md) · [packaging](docs/development/packaging.md) |
| Plugin development | [overview](docs/plugin-development/overview.md) · [mail source](docs/plugin-development/mail-source.md) · [processor](docs/plugin-development/processor.md) · [LLM backend](docs/plugin-development/llm-backend.md) · [notifier](docs/plugin-development/notifier.md) · [storage](docs/plugin-development/storage.md) · [bot exporter](docs/plugin-development/bot-exporter.md) |
| Configuration | [overview](docs/configuration/overview.md) · [i18n](docs/configuration/i18n.md) |
| For AI agents | [invariants](docs/agent/invariants.md) · [module map](docs/agent/module-map.md) · [change playbook](docs/agent/change-playbook.md) |
| Decisions | [ADRs](docs/adr/0001-uv-workspace.md) · [0002-pluggy-pipeline](docs/adr/0002-pluggy-pipeline.md) · [0003-host-independent-core](docs/adr/0003-host-independent-core.md) |
| Build history | [BUILD_LOG](docs/build-log/BUILD_LOG.md) · [简体中文 README](README.zh-CN.md) |

## Plugin marketplace

Browse and install plugins from remote repositories:

```bash
uv run mailflow plugin repo add mailflow-repo https://github.com/Kingcxp/mailflow-repo
uv run mailflow plugin market list
uv run mailflow plugin market show mailflow-notify-ntfy
uv run mailflow plugin install mailflow-notify-ntfy     # restart to load
```

The [mailflow-repo](https://github.com/Kingcxp/mailflow-repo) repository
hosts the marketplace: one folder per plugin, grouped by category, so adding
a plugin is a single pull request that never touches other plugins' files.
Its docs/ folder is the plugin-development guide, and a pull-request
workflow validates exactly the plugins each PR changes.

**Write your own plugin** — the TUI has a new-plugin wizard (Market tab →
New): pick a folder in the directory tree, optionally create a subfolder,
choose the template category (mail source / processor / LLM backend /
notifier / storage / bot exporter), and MailFlow generates a complete,
loadable template. The wizard is also available to hosts embedding the core
via `mailflow.plugin_template.scaffold_plugin`.

**Ship MailFlow as a bot plugin** — the Market tab's Export button opens the
same folder-tree wizard, now selecting a framework (NoneBot, AstrBot, ...)
and exporting a ready-to-install framework plugin from your configured
instance. The command-line equivalent is `mailflow export --framework <id>
--output <dir>`; exporters are plugins, so a new framework is one install
away. The exported plugin embeds the full chat command surface: messages
starting with `mailflow` (NoneBot) or `/mailflow` (AstrBot) are dispatched
to the shared command router, long replies are split into several messages,
and the daily digest is paginated into chat. See
[docs/architecture/bot-export.md](docs/architecture/bot-export.md).

**Localized and styled** — plugins can ship translated one-line
descriptions and markdown readmes (`descriptions` / `readmes` in
plugin.json); CLI and TUI automatically use the variant matching the app
language, and `market show` renders the readme with rich markdown effects
(**bold**, ~~strike~~, `<span style="color:#ff5500">colors</span>`).

## Project layout

```
packages/mailflow-core       host-agnostic domain, pipeline, service facade, bot export
packages/mailflow-bundled    composition root: the official plugin set
packages/mailflow-cli        rich Typer host (run/command/shell/export/...)
packages/mailflow-tui        Textual UI (Mail/Mailboxes/Actions/LLMs/Runtime/Logs/Market/Settings)
packages/mailflow-testkit    deterministic fakes for tests (mailflow-mail-fake is a dev-only source plugin)
plugins/*                    discoverable adapters, processors and bot exporters
configs/ · translations/     example configs and language packs
docs/                        architecture, development, agent documentation
```

## License

MIT. IMAP is built in with presets for QQ, 163, Outlook and Gmail (any
generic server works too) — see
[CHANGELOG.md](CHANGELOG.md).
