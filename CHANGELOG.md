# Changelog

All notable changes are recorded here; the format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Bot platform auto-provisioning** (`GATEWAY_PROVISIONER` component kind,
  `mailflow.gateway.GatewayManager`): the Bots tab now walks through a
  three-step setup — basics (instance name) → provider dropdown (napcat,
  wechaty, plus notifier-only platforms) → guided gateway provisioning.
  NapCat is downloaded and launched automatically; WeChaty installs the
  pad-protocol gateway bridge via npm and runs it as a managed child; both
  show the login QR inside the TUI. Instances are supervised (restart with
  backoff), persisted in storage preferences and resumed on boot.
- The onebot and wechaty plugins now register gateway provisioners
  (`napcat` / `wechaty`) alongside their notifiers; the WeChaty bridge
  reference (`wechaty-gateway.js`) ships in the plugin package.
- NapCat's release version is resolved from the latest GitHub release at
  install time (a pinned version could 404); download errors now include
  the URL and HTTP status.
- The bot setup guide is a bordered dialog with a live log pane
  (timestamp + level, scrollable) and the QR inside the frame, buttons
  outside; the provider dropdown offers `napcat` (auto-deploy) separately
  from the manual `onebot` form.
- ComponentRegistry keys are now (kind, component_id): an id that is both
  a notifier and a gateway provisioner (`wechaty`) no longer fails the
  whole plugin's registration with "already registered".
- The bot guide dialog sizes itself to the terminal (percent-based)
  instead of a fixed box; provider labels distinguish manual OneBot from
  NapCat auto-deploy.
- NapCat installs use a pinned default release (4.18.19) instead of the
  GitHub API, which hit the anonymous rate limit (403) after a few
  calls; the API is only consulted when `napcat_version = "latest"` is
  set explicitly.
- The WeChaty provider label marks it as auto-deploy
  ("WeChat (WeChaty pad protocol, auto-deploy)"); the guide dialog is
  centered (single-child ModalScreen) and its log pane scrolls inside
  the frame.
- Gateway instance ids are sanitized for filesystem paths and ports
  (spaces/parentheses in user-chosen names no longer break installs:
  "A Bot (NapCat)" used to fail with "no entry point found"); a short
  hash keeps distinct ids distinct.
- A manual WeChaty choice ("WeChat (WeChaty manual)") joins the
  auto-deploy one, with a documentation link shown in the form; the
  guide dialog is at least 80% of the terminal width and its log pane
  fills the dialog height (the actions row no longer steals space).
- NapCat provisioning: downloads log progress, the child process writes
  to a per-instance napcat.log (startup failures now show the log tail
  instead of a bare timeout), readiness waits on the WebUI port (6099)
  — the OneBot HTTP API only listens after the QQ session logs in, which
  is now stated in the error. Already-running gateways on the target
  port are reused instead of starting a conflicting process.
- NapCat auto-deploy works on headless Linux (Debian containers):
  installs xvfb + xauth + the Linux QQ deb, places NapCat.Shell inside
  QQ's resources/app, patches the app entry (official BootWay03 flow)
  and launches `xvfb-run qq --no-sandbox`. Windows keeps the inject
  mode via NapCatWinBootMain.exe. Without a QQ client the error now
  says so immediately instead of timing out. MailFlow never installs
  system packages itself: missing xvfb/QQ are reported with the exact
  install command and the flow fails cleanly.
- The notifier form distinguishes gateway platforms from manual ones:
  auto-deploy platforms (napcat/wechaty) continue via Next into the
  guided setup; manual platforms (onebot, wechaty-manual, openclaw)
  get a connection-test button that probes the endpoint and unlocks
  Save only after a successful test. Save is disabled until then.
- NapCat install validation checks for the real entry point
  (napcat.mjs); a partially removed gateway directory now fails with a
  clear "reinstall" hint instead of a bare no-entry-point error.
- New cleanup targets: `make clean-gateways` (delete data/gateways/ so
  a broken gateway install can be redone) and `make clean-config`
  (delete local config files so they regenerate from the example); both
  ask for confirmation interactively and accept -y.
- NapCat QR login: the OneBot `get_qrcode` endpoint does not exist in
  NapCat (it was a 404 all along), so the guide now reads the login QR
  NapCat writes to `<workdir>/cache/qrcode.png` (NAPCAT_WORKDIR is
  pinned to the instance dir). NapCat refreshes that file when the QR
  expires, so the TUI shows the new QR automatically; login is still
  decided by get_login_info, never by QR absence. WeChaty's bridge
  already re-emits `scan` with a fresh QR on expiry.
- WeChaty auto-deploy no longer requires a pad-protocol token: the free
  web-protocol puppet (wechaty-puppet-wechat4u) is installed alongside
  padlocal and the bridge picks it when no token is configured —
  scan-to-login, no platform auth (ban risk: use a disposable account).
- QR-related guide texts are provider-aware ("{provider} is starting")
  instead of hardcoding NapCat; when no QR appears the guide shows the
  provisioner's diagnosis (QR file path + napcat.log tail) instead of
  waiting silently.
- Fix: NapCat's NAPCAT_WORKDIR and log file pointed at the QQ app dir on
  Linux (after target was switched to the run dir), so the QR cache and
  logs went to a root-owned path and qr()/tail never saw them. They now
  use the instance data dir (data/gateways/<id>) consistently.
- Fix: cancelling the gateway guide froze the TUI — terminate() blocks up
  to 5s waiting for the process; it now runs off the event loop and kills
  the whole process tree (taskkill /T, pkill -P) so no orphan QQ remains.
- WeChaty: Tencent shut down the web protocol, so the token-free
  wechat4u puppet is kept only as a best-effort fallback (may work for
  some accounts; ban risk documented). The bridge now reports an error
  with the real reason when no QR appears within 60s instead of showing
  a misleading 'scan' prompt, and the guide only shows the scan prompt
  once a QR payload actually exists.
- NapCat on Linux: the QQ Electron client needs a session bus; the
  launcher now runs under `dbus-run-session` and the runtime check
  requires dbus (apt install dbus dbus-x11) — the old failure mode was
  a cryptic dbus/bus.cc error with no QR and an empty log.
- Process-tree teardown on Linux is recursive (pgrep walk) so
  dbus-run-session > xvfb-run > QQ chains are fully killed.
- New gateway: `openwechat` — WeChat scan-to-login with no platform
  token. The provisioner builds a small Go bridge on install (requires a
  Go toolchain; reports the exact apt command when missing); the bridge
  renders the login QR to a PNG served at /qr (shown inline in the
  guide), hot-reloads the session across restarts, and exposes
  /health + /send for the notifier. Added as a provider choice in the
  Bots form (mailflow-notify-openwechat).
- NapCat on Linux now installs the official AppImage (QQ NT + NapCat
  bundled, ~190 MB) instead of the QQ deb + /opt/QQ mirror + dbus:
  only xvfb + fuse are needed, and the QR cache lives next to the
  AppImage in the instance dir. The Shell zip flow stays for Windows.
- The WeChaty bridge renders the scan QR text to a PNG (qrcode npm
  package) so the guide shows a real scannable QR — previously a bare
  URL appeared for users with no route to the gateway host.
- The gateway guide now shows a live download/install progress bar:
  GatewayManager injects a shared InstallProgress into the provisioner's
  install options; provisioner download loops report bytes/percent
  (Content-Length when available), and the guide polls and renders it
  (NapCat AppImage ~190 MB, Shell zip, openwechat Go build stages). The
  bar lives inside the log pane and throttled lines (one per 5%) scroll
  through the log.
- Fix: NapCat on Linux downloaded the AppImage but start still looked
  for a node entry point (napcat.mjs) and failed with 'no entry point
  found'. Start now runs the AppImage directly on Linux and only
  resolves the Shell-package node entry on Windows.
- Fix: launching the AppImage failed with '...AppImage: not found' —
  the entry path and child cwd were relative, so the child resolved
  data/gateways/... against its own cwd and double-prefixed. Both are
  now absolute.
- The install progress bar widget is gone; progress is a single line at
  the top of the log pane, updated in place (never appended to the
  stream).
- NapCat AppImage install no longer hard-fails when the GitHub release
  API is unreachable: it falls back to the pinned known release URL
  (v4.18.19) with a warning, and the latest-asset lookup now also
  carries the asset name (previously the file kept the old pinned
  name).
- NapCat AppImage launch uses --appimage-extract-and-run, which unpacks
  to a temp dir and needs no FUSE — containers/VMs that cannot modprobe
  fuse (PVE, many cloud hosts) no longer fail. fusermount absence is a
  warning, not an error.
- Install progress lines now name the file being downloaded
  ('downloading: QQ-...AppImage — 45 / 190 MB').
- The guide QR is much smaller and module-aligned: sampled from the
  centre of each QR module (<=32 modules = 64 columns x 32 rows) so the
  whole code fits the dialog and stays scannable — previously the 2px
  sampling blew it up past the pane width and only a few rows showed.
- The gateway guide now treats QR diagnostics (e.g. 'QR file not
  created yet') as wait states, not fatal errors: the first AppImage
  launch is slow (extract + QQ boot), so the loop keeps polling for up
  to 5 minutes instead of aborting at the first missing file.
- The QR and download progress now live inside the log stream (no
  separate widgets), and the dialog/log pane fills the modal height so
  no space is wasted below the title.
- Fix: RGB (3-channel) QR PNGs rendered as one line of white blocks —
  the pixel stride was hardcoded for RGBA. The stride now honors the
  IHDR color type (gray/RGB/RGBA), and all three render correctly.
- The QR is back in its own panel above the log (capped at 29 modules =
  58 cols x 29 rows) so it never overflows the log pane height.
- Fix (real root cause): the QR renderer decoded raw scanlines without
  applying PNG row filters (Sub/Up/Average/Paeth), so real QR PNGs —
  which use filters — came out as a few rows of noise/white. _ascii_qr
  is now a full minimal PNG decoder: it merges multi-chunk IDAT,
  applies all five filter types, honors the IHDR color type
  (gray/RGB/RGBA), and samples module centres.
- The QR decoder also handles sub-byte bit depths now: NapCat's
  qrcode.png is a 147x147 4-bit grayscale PNG, which previously
  reported 'unsupported png 147x147'. 1/2/4/8-bit depths and palette
  (PLTE) images decode correctly (verified with a 147x147 4-bit
  filter-encoded fixture).
- The guide layout is more compact (title/status/actions each 1 row,
  dialog 92% x 95%) and the QR is capped at 25 modules (50 cols x 25
  rows) so the whole code fits a ~30-row terminal without truncation.
- The QR renderer is now actually scannable (verified end-to-end with a
  real QR decoded by jsQR): it detects the true module size from the
  PNG's finder pattern instead of guessing a step (which misaligned and
  destroyed the code), skips the margin, and renders with half-block
  characters (each terminal row = 2 module rows), so a 29-module QR is
  ~15 rows tall and fits the panel whole. Buttons and title are pinned
  to 1 row in on_mount (CSS-only height rules are overridden by
  Textual's Button variant styles), and the QR panel is centered.
- Chat command flow: new `general.command_prefix` setting (default /)
  and a local bot endpoint (127.0.0.1:18789/bot/message, auto-bumping
  port on conflict) that dispatches prefixed messages through the
  CommandRouter; the WeChaty bridge now forwards incoming chat messages
  there and replies back in the same chat. Messages without the prefix
  are ignored, so normal conversation is never treated as commands.
- NapCat login probe now checks both the OneBot port and the WebUI port
  and logs the probed endpoint/status on change, so a misconfigured
  port is visible in the guide instead of silently never enabling Done.
- The guide QR poll interval is 5s (was 3s) to reduce memory churn on
  low-RAM VMs where the QQ client already uses ~1.5 GB.
- NapCat login detection is more robust: get_login_info accepts any id
  field (user_id/uin/account), probes both the OneBot and WebUI ports,
  and falls back to 'QR file unchanged for 90s + HTTP reachable' as a
  logged-in signal after a successful scan — Done should now enable
  right after scanning. The QR panel is vertically centered and the
  waiting text explains that login confirmation follows the scan.
- The guide adds an 'I'm logged in' button (enabled once the QR is
  shown): if automatic login detection never fires, the user confirms
  the phone login manually and the flow finishes — no more being stuck
  on 'waiting for login confirmation'. The QR panel is centered in both
  the component CSS and app.tcss (the app-wide sheet previously
  overrode the alignment), with top padding so it does not hug the
  title.
- Removed the stable-QR login heuristic: while waiting for a scan the
  QR file is equally stable, so it falsely reported 'logged in' before
  the user scanned. Login is now only confirmed by get_login_info or by
  the manual 'I'm logged in' button.
- The 'I'm logged in' button is styled like the others (1 row, spacing)
  — it previously used the default 3-row Button style and cramped the
  row.
- Saving a guided notifier updates an existing entry with the same id
  instead of failing with 'duplicate notifier_id' (a previous failed
  attempt could leave a stale entry behind).
- The guide's 'I'm logged in' button now actually finishes the flow:
  the result was only set after the QR loop returned (which blocked for
  minutes), so Done did nothing and the QR kept refreshing. The result
  is set right after provisioning, the QR loop exits on the button, and
  pressing 'I'm logged in' dismisses the guide with the result in one
  step — verified end-to-end.
- Fix: bots never appeared in the list after adding — refresh_data and
  on_mount called a non-existent _render() (AttributeError swallowed by
  Textual), so the table never repainted. They now call _render_rows().
- The Bots tab gains an Edit button: select a row and edit targets
  (e.g. group:<id> / user:<qq> subscriptions) or other options via the
  pre-filled notifier form — this is how you subscribe a chat.
- 'I'm logged in' now only enables Done (Done remains the explicit
  final confirmation); buttons get grid-gutter spacing.
- Gateway supervision polls every 60s (was 30s) and the NapCat AppImage
  launch caps the Electron heap at 1 GB (--max-old-space-size) to cut
  CPU/memory pressure on low-RAM VMs that made the TUI lag.
- Chat subscriptions are command-driven: `<prefix>mailflow help /
  subscribe / unsubscribe / status` work inside groups and private
  chats. Only users listed in the notifier's `admins` (QQ number / wxid,
  one per line in the bot form) can run commands; subscribing adds the
  chat to the notifier targets so mail notifications land there.
  Subscriptions persist across restarts (gateway.sub.* preferences).

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
- IMAP polling watermarks are now persisted (per-account, in storage
  preferences): a restart resumes where polling left off instead of
  re-seeding at the newest UID, which silently skipped every mail that
  arrived while the service was down. The runtime injects the store into
  sources that expose `set_watermark_store`.
- The built-in LLM processor tolerates a missing `urgency` in the model's
  JSON (defaults to `info`): a single omitted field no longer fails the
  whole mail's analysis.


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
