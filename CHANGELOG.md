# Changelog

All notable changes are recorded here; the format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- VS Code-style settings editor: `mailflow.settings` turns the typed schema
  into sections (MailFlow's own groups plus one per plugin that owns
  configured components), per-option editors derived from the field type,
  schema defaults, restore-to-default, and validation errors that name the
  offending option. The TUI Settings tab is a search box, a section sidebar
  and one card per option (name, localized description, inline editor,
  Save / Restore default); list and mapping values open a line editor and
  structured entries open a real form window.
- Dedicated **Mailboxes** and **LLMs** tabs: add, edit and delete mail
  accounts and LLMs from forms instead of hand-editing TOML. For LLMs the
  list order *is* the fallback chain — the first entry is the default and
  each entry falls back to the ones below it, so `default`/`fallback` are
  derived; deleting an LLM scrubs every reference to it.
- Mailbox history browsing: a mail source may implement the optional
  `HistoryCapableSource` capability (`fetch_history(limit, offset)`), and the
  Mailboxes tab pages through mail that arrived before MailFlow was
  configured. Selected mails run through the pipeline via
  `service.process_mail`, taking the same path as live mail (dedup,
  persistence retry, `mailflow.mail.processed`, notifier thresholds);
  already-stored mail is marked and skipped. Implemented for IMAP without
  disturbing the incremental poll water-mark.
- 70 localized option descriptions (English + Simplified Chinese) generated
  by `tools/gen_option_descriptions.py`, so every setting documents itself in
  the Settings UI and in `config list`.

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
- Built-in `mailflow-mail-imap` mail source: IMAP polling + SMTP replies
  with provider presets for QQ, 163, Outlook and Gmail (and generic
  school/work servers via explicit hosts); MIME parsing (text/html bodies,
  encoded subjects), credentials from `${ENV_VAR}` placeholders.
- Built-in `mailflow-llm-anthropic` LLM backend: Claude Messages API
  (`provider = "anthropic"`), registered alongside the OpenAI-compatible
  backend in `mailflow-bundled`.
- Marketplace defaults: the official `mailflow-repo` repository ships
  configured (an explicit `repositories` list replaces it); TUI `ReposScreen`
  manages remote repositories; plugin details show author and last-updated;
  the Market tab buttons no longer overflow (two control rows).
- `LLM_ENHANCER` component kind: processor plugins customize the built-in
  LLM analysis through `system_prompt` chaining, `extra_messages` and
  `post_process` — bounded customization without reimplementing
  classification.
- `llm_enhancer` scaffold template category and marketplace category.
- Summary language: `general.summary_language` pins the language of LLM
  summaries/reasons/replies (empty follows the interface language); the
  built-in `llm-importance` processor injects the instruction per mail.
- Config descriptions are localized (`config.desc.*` keys in en/zh-CN).
- TUI: selecting a plugin row opens a full-screen VS Code-style detail
  (title, metadata, markdown readme, install/uninstall/enable/disable);
  selecting a config row opens an edit form (value, cancel/save).
- The docs gate (`tools/check_docs.py`, `make docs`) now verifies that
  documentation matches the code, not just that files exist: quoted
  repository paths, `make` targets, `mailflow.*` event names,
  `service.<method>(` references and `mailflow-*` plugin ids must all resolve.
  `CHANGELOG.md` and the build log are exempt as historical records.
- Locale hygiene is test-enforced: en/zh-CN key parity, no duplicate lookup
  paths, no Chinese text left in `en.json`, and a `config.desc.<key>` entry
  for every configurable option in both packs.

### Added

- **Reject with a reason** in the Mail tab: disputing a mail's priority
  records the reason as a lasting correction guideline that is injected
  into every future LLM analysis, so the same mistake is not repeated.
  The detail view shows the rejection reason; remote mode is unsupported.

### Fixed

- More UI-blocking operations moved off button handlers into exclusive
  workers: the Bots tab connection probes (now also run concurrently
  instead of serial 8s timeouts), the reply modal's prepare (LLM) and
  confirm (SMTP) steps with staged status text, market
  install/uninstall/enable/disable (pip + runtime rebuild), and the
  Runtime tab's plugin actions. Bulk history re-analysis refuses a second
  click while a batch is running.


- Bulk history browsing beyond ~100 mails could crash or freeze the app:
  page loads now run in an exclusive worker (IMAP I/O never blocks the UI
  handler), duplicate message-ids across windows are collapsed to one row
  (a repeated DataTable row key raised), a poison mail renders as a
  "(parse error)" row instead of killing the pane, and the per-mail full
  refresh after a bulk re-analysis is coalesced into a single pass.


- The entry form's test button ran the LLM probe for every group — testing
  a mailbox surfaced "no llm_backend component 'imap'". It now dispatches
  per group (accounts → IMAP login check, llms → completion ping).
- The LLM connection test reported the unformatted template (the key
  expects a {reply} placeholder the code never passed); it now uses the
  model variant: latency + model name.
- Saving an LLM/account could leave the table stale until the next manual
  refresh: persisting awaited the runtime rebuild, whose source-task drain
  waited indefinitely for a source parked in a blocking connect. The drain
  is now bounded (5s) and IMAP connects carry a 20s socket timeout.
- Language switches left the Logs tab label untranslated (the logs pane is
  deliberately never remounted, and the relabel pass skipped it).


- Locale keys added flat at the top level of the language packs (all UI
  strings introduced in the previous rounds) were silently ignored by the
  pack loader, which only reads the nested `messages` object — buttons and
  titles rendered as raw keys. All keys migrated into `messages`.


- Deleting a mail-derived todo now hides it permanently (stable natural
  key: mail id + due time + type survives re-analysis); custom todos are
  deleted for real. The Actions tab gained a Delete button.
- History browser pick column renders again (Rich markup was swallowing the
  `[x]`/`[ ]` cells); picked/unpicked are localized plain-text marks.
- Switching `general.language` hot-rebuilds the pipeline, so summaries and
  notes follow the UI language immediately instead of after a restart.
- IM bot forms: provider is a dropdown (onebot/wechaty/openclaw-weixin)
  with per-provider option fields and bilingual descriptions; the Bots tab
  explains that QR login happens in the external runtime.
- LLM activity is visible again: router successes and backend retries log
  at INFO into the main Logs tab; the LLM tab's lower half is now a colored
  per-mail notification feed.
- New IMAP accounts skip the existing backlog by default (first poll seeds
  the watermark without emitting); `analyze_backlog = true` restores the
  legacy pull. The `other` todo category is a last resort via prompt-level
  category definitions.


- The TUI and the CLI `run`/`shell` commands never forwarded `--config` to
  `start_service`, so `service.config_path` stayed `None` and every
  persisting action (`config set`, plugin enable/disable, repository
  management) failed with "no config file loaded".
- `write_config` materialized credentials into the TOML file: a value that
  came from a `${VAR}` placeholder and an `api_key` resolved from
  `api_key_env` were both written back as plaintext. Placeholders are now
  recorded at load time and restored on write; env-backed keys are blanked.
- `patch_config_value` fell back to a whole-file scan when the target section
  was missing, so patching `general.language` rewrote `[i18n].language`
  instead. The scan is section-scoped and reports failure so the caller does
  a full rewrite.
- The TUI subscribed to `mail.processed` while the runtime emits
  `mailflow.mail.processed`, so panes never auto-refreshed after a mail was
  processed. Docs and README examples advertised the same wrong name.
- The IMAP source advanced its UID water-mark *before* fetching a message, so
  a transient FETCH error skipped that mail permanently. The mark now moves
  only after the message is parsed, and the loop stops at the first failing
  UID so the next poll retries it.
- Marketplace refresh blocked the UI and re-fetched over the network on every
  search keystroke; the fetch now runs in an exclusive worker and filtering
  renders from cached entries.
- Buttons drawn on Textual's `ansi_default` variant (black-on-black in dark
  terminals) now carry explicit variants, and modal dialogs — including the
  repository manager — have a visible Back button instead of escape-only.
- `i18n.t()` ran `str.format` even without parameters, so a message
  containing literal braces (a `${ENV_VAR}` example) logged a format error;
  eight English keys that contained Chinese text are now English.
- Locale packs defined 21 keys as literal dotted strings (`"tui.btn_cancel"`)
  instead of nested objects, and one of them duplicated an existing nested
  key. Both spellings flatten to the same lookup path, so iteration order
  decided which value won and editing the other silently did nothing; all of
  them are now properly nested.
- Documentation named things that no longer exist: the module map and change
  playbook still listed the two processor plugins as plugins (they moved into
  core), `llm.md` pointed at a deleted plugin path, `deployment.md`
  documented a `make doctor` target that never existed, and `tests.md`
  described a layout missing 9 of the 19 test files.
- Two leftover `plugins/mailflow-processor-*` directories held only orphaned
  `.pyc` files, making deleted plugins look installed on disk.

### Changed

- Classification pipeline is built into `mailflow-core`: `rules` (10) and
  LLM `llm-importance` (20) processors run by default; the old
  `mailflow-processor-rules` / `mailflow-processor-llm-importance` plugins
  are removed. A plugin registering the same component id replaces the
  built-in step.
- TUI buttons: Textual 8 renders button labels on ansi-default (black)
  backgrounds and fixed `height: 3` overflowed the 3-row containers,
  collapsing labels; buttons now use flat high-contrast variants and
  auto height. All tables select whole rows (`cursor_type: row`).
- Removed `MAILFLOW_FROM_ZERO.md` (reconstruction plan archived into the
  build log); all references updated.

### Planned provider phase (not implemented in 0.1.0)

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
