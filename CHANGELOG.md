# Changelog

All notable changes are recorded here; the format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Mail deduplication: `normalized_message_id` is account-independent
  (provider id → RFC message id → content digest) and the runtime skips
  already-stored or in-flight copies, so a mail forwarded to several
  configured accounts is processed, stored and notified exactly once.
- Chat-first command surface: `mail list`/`action list`/`plugin list`/
  `help` are paginated (10 rows per page) with wrap-friendly layouts,
  `mail list --query` filtering, and unique-prefix ids for every
  `mail`/`action` operation.
- `feedback <mail_id> <reason>`: user notes roll into guidelines injected
  into every LLM analysis, so the model adjusts its filtering strategy with
  a grounded rationale; `mail show` displays the note.
- Daily 08:00 digest (`mailflow.action.digest`): today/upcoming counts plus
  approaching action items, once per day, for the host to paginate.
- Updates: MailFlow releases via the GitHub API and plugin versions via the
  marketplace for the recorded update source (local installs and removed
  repositories are never auto-updated); `update check|now|status|auto on|off`
  and a daily auto-update loop (`general.auto_update`, default on).
- Local plugin installs: `plugin install <path>` (single plugin folder or a
  batch of folders) and a TUI file-tree installer (Market → 从文件夹安装).
- Chat bridge in exported bot plugins: `mailflow ...` (NoneBot) /
  `/mailflow ...` (AstrBot) messages dispatch to the shared command router,
  long replies split into several messages, the digest is paginated.
- Declarative plugin API (`mailflow.plugin_api.define_plugin` + decorators)
  and scaffold templates that ship a uv-ready dev environment
  (`uv sync --group dev`); uv install instructions for all three platforms.
- docs/development/deployment.md: Windows/Linux/macOS walkthrough.
- Formal-letter reply templates (Chinese / English): `reply compose
  <mail_id> <cn|en>` (or the TUI template buttons) pre-fills the draft with
  an opening, body, closing and a right-aligned signature block, the date
  filled automatically. The TUI reply editor gains a toolbar — bold,
  italic, and left/center/right alignment — and chat clients can type the
  same constructs directly: `**bold**`, `*italic*`, `<right>…</right>`,
  `<center>…</center>`; `reply show` renders a plain-text view.
- `mailflow-notify-telegram` marketplace plugin: Telegram Bot API notifier
  (urllib only, skips gracefully without credentials).
- User-created timed action items: `action add "<summary>" --due
  "YYYY-MM-DD HH:MM" [--type <type>] [--notes "..."]` and
  `action delete <item_id>`; custom items persist in the storage backend,
  appear in `action list|show` with source "user", and enter the reminder
  scheduler like mail-derived items (`mailflow.action.reminder` events fire
  for them too).
- Bot-framework export: `BOT_EXPORTER` plugin kind with
  `registrar.add_bot_exporter(framework_id, factory)`; `mailflow.bot_export`
  (`BotExportContext`/`BotExportResult`, `export_bot_plugin`) shared by the
  CLI, the TUI export wizard and make targets; built-in NoneBot2 and AstrBot
  exporters (`plugins/mailflow-export-nonebot`,
  `plugins/mailflow-export-astrbot`) registered in `mailflow-bundled`.
- `mailflow export --framework <id> --output <dir>` command; `make
  bot-plugin` / `bot-plugin-nonebot` / `bot-plugin-astrbot` targets.
- TUI export wizard (`BotExportScreen`): framework select, directory tree
  with optional subfolder creation, export button on the Market tab.
- `bot_exporter` scaffold template category (TUI wizard + plugin_template).
- `mailflow-repo` marketplace: `bot_exporter` category with the two exporter
  plugins, bot-exporter developer guide, validator support for the new kind.
- Documentation: `docs/architecture/bot-export.md`,
  `docs/plugin-development/bot-exporter.md`, full Simplified Chinese README
  (`README.zh-CN.md`).

### Changed

- Removed `MAILFLOW_FROM_ZERO.md` (reconstruction plan archived into the
  build log); all references updated.

### Planned provider phase (not implemented in 0.1.0)

- Generic IMAP source adapter (incremental UID tracking, MIME normalization)
- Gmail API source (OAuth, history sync, thread-aware replies)
- Outlook / Microsoft Graph source (delta sync)
- Production notification adapters (ntfy, Gotify, Telegram/QQ bridge callback)

## [0.1.0] — framework baseline

- `mailflow-core`: four-level urgency contract with public colors; normalized
  mail domain; typed configuration with `${ENV_VAR}` interpolation; component
  contracts and an ownership registry; pluggy-based plugin discovery; async
  event bus; named LLM routing with ordered fallback and secret redaction;
  ordered processor pipeline with timeout/retries/failure policy and a
  fallback-summary guarantee; queue-based rich logging with secret redaction;
  English/Chinese JSON localization with external packs; bounded async runtime
  with per-account source isolation and the daily 04:00 retention scheduler;
  embeddable service facade with snapshots, urgency mutations, persistent
  language and the confirmed reply workflow; transport-neutral command router.
- `mailflow-bundled`: composition package registering the official plugin set
  via static imports (frozen-safe) with optional entry-point discovery.
- `mailflow-cli`: `run`/`command`/`shell`/`config-check`/`snapshot`/`doctor`/`tui`.
- `mailflow-tui`: Textual interface with Mail, Actions, Runtime, Logs and
  Settings tabs; search, urgency-colored table, reply modal with
  prepare/confirm gating, language selector.
- Plugins: fake mail source, sqlite storage with seven-day recovery trash,
  OpenAI-compatible chat completions backend, rules processor, LLM importance
  processor, console notifier.
- Tooling: uv workspace, Makefile gates (test/lint/format/mypy/pyright/build/
  exe), Nuitka standalone/onefile executables, docs gate.
- Tests: 100+ unit, integration (monkeypatched httpx — no real API calls) and
  end-to-end tests through the public `start_service` entry point.
