# Changelog

All notable changes are recorded here; the format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Re-analyze** in the Mail tab: re-run the selected mail through the
  pipeline, or one-click re-analyze every mail whose analysis did not
  complete (no analysis or any failed processor note) — with the same
  per-mail progress reporting as the history browser.


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

### Changed

- Mail tab controls: the three selects (manual urgency / filter / sort)
  share the row with a two-row equal-width button container; the urgency
  dropdown mirrors the selected mail and its options are localized.
- Market tab: sort dropdown (name/status/category/installed-first/
  enabled-first/not-installed-first), localized category filter labels,
  locally installed/bundled plugins appear as entries with their
  docstring as the detail readme, plugin-readme links are clickable and
  route through `general.browser_mode` (system / Carbonyl terminal
  rendering / disabled).
- Runtime tab: double-clicking a plugin row opens the same plugin-detail
  dialog as the Market tab (a DataTable subclass recognizes the second
  click of the chain — the stock widget stops Click events at the table).
- All panes load asynchronously on mount (exclusive workers) with
  serialized refresh locks; the mail table renders in 50-row chunks so
  large mailboxes never freeze the UI.

- Plugin detail dialogs (Runtime tab and Market tab) open instantly:
  double-clicking a plugin row no longer triggers a marketplace network
  fetch. Runtime rows fall back to the locally loaded plugin metadata
  (module docstring readme) when the market has not loaded yet, and both
  panes guard against the double-click firing the dialog twice.
- `make clean` no longer deletes user data: `data/` (mail database,
  trash, preferences) and `logs/` are preserved; only caches and build
  output are removed.
- `action add <summary> --due 2026-09-01 09:00` parses the unquoted
  date+time pair correctly (the time token was previously dropped and
  the command always failed with "invalid due time").
- Re-analyzing a history mail (`process_mail(force=True)`) emits
  `mailflow.mail.processed` exactly once (it was emitted twice — once by
  the pipeline path and again by the force wrapper).
- A fired reminder marker is persisted before the reminder event is
  emitted, so a crash or a failing event handler can no longer cause the
  same reminder to fire again on the next tick.
- The daily auto-update check marks the day as checked only after the
  check succeeds; a transient network failure now retries later in the
  day instead of silently skipping.
- Uninstalling a plugin also removes config entries (accounts, LLMs,
  processors, notifiers) that referenced its components, instead of
  leaving them to be silently skipped on every reload.
- Enabling a plugin that was disabled (or installed since startup) now
  auto-creates its notifier instances: the auto-instance scan builds the
  registry the way the reload will, so components of a not-yet-loaded
  plugin are found.
- `move_entry` shifts env-placeholder paths for *all* affected entries
  (multi-step moves previously remapped only the two endpoints, so
  `${VAR}` secrets could be written back under the wrong LLM).
- The Anthropic LLM backend no longer includes the response body in
  error text (the body can echo the API key into persisted processor
  notes); errors carry the status code only.
- The exported NoneBot plugin module had a duplicate `zoneinfo` import
  and a mis-indented digest comprehension that would not compile; both
  are fixed.
- The OneBot notifier's "not configured" log referenced a nonexistent
  `_config_id` attribute; it now logs the record id.


- Urgent notifications survive transient transport failures: the runtime
  retries each notifier three times (2s/4s backoff) before logging the
  loss, and the OneBot notifier validates numeric QQ ids at construction
  so one malformed target line can no longer abort delivery to the
  remaining targets.
- The todo detail modal now loads the source mail: subject, sender, date,
  analysis summary and reason, plus the original body (html rendered as
  text) — a todo without its mail context forced manual hunting through
  the mail tab.
- The settings search filter e2e re-queries the search input every poll
  iteration: a late remount detaches the captured widget and the filter
  never applied on slow runners.


- The re-analyze buttons and the urgency dropdown rendered raw locale keys
  (`tui.btn_reparse`, …): a workspace sync issue kept dropping freshly
  written key batches from the language packs. All keys are re-pinned and
  a **locale completeness test** now scans every `t()` call site in the
  TUI/CLI source and fails when a referenced key is absent from either
  pack (plus zh/en parity) — this class of silent failure is caught by CI
  from now on.
- The manual-urgency dropdown: the widget tooltip no longer lingers over
  the open overlay (Textual's Select cannot attach per-option tooltips, so
  the explanation lives in a persistent localized hint line below the
  controls instead of crammed into option labels), and the bot status
  probes are localized (logged-in-as / online / unreachable / …).


- Resetting a mail's manual urgency back to automatic crashed the app: the
  Select's blank sentinel flowed into `Urgency(...)` and raised. The
  dropdown is now allow_blank with an explicit localized "follow automatic"
  entry (first, so the mount-time auto-selection routes into the harmless
  reset path), the handler tolerates blank/unknown values, programmatic
  changes (mount, relabel, selection sync) never stamp overrides, and the
  dropdown stays in sync with the selected mail's actual override.
- LLM responses that bend the JSON schema (a null summary, string
  booleans, non-list action_items) no longer fail the whole analysis:
  every field is coerced, malformed action items drop individually, and a
  truly unparseable payload is logged with an excerpt so prompt problems
  are visible in the Logs tab.


- Canned rules-processor phrases ("Advertisement detected by rules",
  "matches advertising keywords", the important-sender reason) are stored
  as English data but now display localized at render time — under zh-CN
  they read 规则判定为广告 / 命中广告关键词. Free-form LLM text passes
  through untouched.
- The mail detail view shows failed processor notes (e.g. an LLM rate
  limit), which explains mails whose analysis carries no reason: the
  failure used to be silent.


- Summaries ignored the persisted UI language after a restart: the
  pipeline was built from the `[i18n]` bootstrap default (typically `en`)
  before the stored zh-CN preference was ever applied, and the persisted
  preference was never propagated into the processor chain. Startup now
  reads the preference before constructing the pipeline (storage
  initialize is idempotent so hosts can bootstrap early), and
  `general.language` takes precedence over the `[i18n]` bootstrap value.


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
