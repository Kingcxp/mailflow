# Textual TUI

`packages/mailflow-tui` is a Textual client of the Core service — it renders
service data and calls service methods; no business logic lives in the UI.

## Runner

`mailflow_tui/runner.py` builds the bundled plugin manager, starts one
service with an injected `TuiLogHandler` (records into a queue), runs the
Textual app on the same event loop, and stops the service in `finally`. It
forwards `--config` as `config_path`, which is what makes every persisting
action (settings edits, plugin enable/disable, repository management) work —
without it the service has no file to write to.

## Boot splash

`mailflow_tui/splash.py: SplashScreen` is a full-screen animation shown for a
moment at startup when the runner enables it (`MailFlowApp(..., splash=True)`;
headless tests keep the default `splash=False` so they land directly on the
main screen). It renders a large block-letter "MAILFLOW" wordmark tinted by a
flowing wave of the four urgency contract colors plus the accent (one colour
per glyph), a hairline divider, the localized tagline, a small animated
equalizer bar, a localized status line that advances (loading plugins →
starting service → ready) and the version; Escape skips it, and it pops
itself after ~3.5s. All animation timers are created on the screen so Textual
tears them down with the screen — nothing keeps ticking after it closes.

## Tabs

- **Mail**: one free-form `Input` plus a **Smart action** button; the same box
  filters as you type (subject/sender/summary/body) and is the instruction the
  button sends. **Smart action** calls `service.smart_action`: the model
  classifies the instruction, so "find/filter …" returns the ranked matching
  mails while "add … to my schedule" both matches the mail announcing the
  event and schedules it (`SmartActionIntent`). The urgency-colored
  `DataTable` (■ + a localized urgency label in the contract color) fills the
  pane height; the scrollable detail pane follows the highlighted row (single
  click or arrow keys — no double-click needed) and shows summary, reason,
  action items, the **original body** (HTML rendered as text, binary
  attachment payloads detected and replaced by an explanatory note, bodies
  truncated at 4000 chars), attachment metadata (name/type/size), and a
  localized analysis status. The stored summary and reason are always shown —
  the mail's actual content — and a storage fallback is labelled as not
  generated instead of being hidden, while failed processors list the real
  cause. All stored timestamps are projected into `general.timezone`,
  including action windows. The bottom controls are one row: three dropdowns
  (manual urgency — a localized
  `ad/info/important/urgent/follow-automatic` dropdown that mirrors the
  selected mail — plus urgency filter and sort) taking two thirds of the row,
  and a two-row button container (refresh/trash/Ask & Correct then
  reply/re-analyze/re-analyze-failed) taking the remaining third with
  equal-width buttons, so long localized labels keep their spacing instead of
  being squeezed into slivers. A third row holds the bulk and personalization
  actions: **Clear expired mail** (asks first, then moves mail that has nothing
  left to act on to the trash — completed analysis, no future deadline in the
  mail or in the schedule entry it created, classified ad or info, never
  manually re-classified, and at least a day old when it is an ad; the dialog
  states the count, the service re-checks every record at delete time, and the
  trash keeps it restorable), **Re-analyze all** (asks first, naming the real
  mail count, then re-runs the pipeline over every stored mail — while it
  runs, that same button becomes **Stop re-analysis** and ends the job after
  the mail in flight, reporting how many were analyzed; **Re-analyze failed**
  works the same way, only one bulk run can be in flight (the other button
  parks), and the started job always restores both buttons, so a stopped or
  cancelled run can never leave a control that refuses to start again) and
  **Mail preferences** (below). Both bulk actions run on the *app's* worker, so a pane
  remount from a language switch cannot cancel a half-finished purge, and a
  failure (e.g. a remote service that does not implement the call) is reported
  in the operation status instead of doing nothing. **Re-analyze
  failed** requires no selected row; a mail whose re-analysis fails is counted
  as failed (with the model's own error) and keeps its previous analysis; it
  works through every failed record and
  keeps its live progress/final outcome in a dedicated status widget, so the
  `mailflow.mail.processed` refresh cannot erase feedback. An empty view shows
  a hint (no mail yet vs. no match for the search/filter) and clears the detail
  pane; any results-only render also refreshes detail for its selected result.
  Reply opens the confirmation-gated modal. **Smart action**'s hint line
  reflects real warm-up and completed batches without being overwritten by the
  spinner; a malformed or unavailable batch remains visibly incomplete rather
  than masquerading as an empty result. The running button stays clickable as
  Cancel; cancellation clears the free-form query and restores the complete
  mailbox. Candidate refs (not raw mail ids) make model selections robust, and
  each phase numbers the mails it was given, so a ref is never reused across
  phases. Chat platforms use the same engine via `mail search <need>` and
  retain global `#` handles.
- **Mailboxes** (`settings.py: AccountsPane`): accounts table with
  Add / Edit / Delete (forms, not TOML editing; **double-click or Enter on a
  row opens the edit form**, same for the LLM and notifier tables) plus the
  **history browser** —
  Load history pages a mailbox newest-first through
  `service.fetch_history(account_id, limit=, offset=)`, rows are toggled with
  Enter/click, and *Analyze selected* runs only the picked mails through
  `service.process_mail`. Already-stored mail is marked and skipped, so
  re-analyzing is a no-op instead of a duplicate. Sources that do not
  implement the optional history capability report that instead of failing.
- **Mail preferences** (`profile.py: UserProfileModal`): the recipient
  describes who they are and which mail matters to them (course/exam/lab
  notices, internships, research opportunities — or what to ignore). The text
  is stored once (`feedback.profile`) and handed to the model with **every**
  analysis and smart action, so classification and matching follow that
  person's situation instead of a generic student's. It is user-authored
  context and travels in the user message (data about the recipient), never in
  the system prompt, so a mail cannot impersonate it as an instruction. An
  empty save clears it.
- **Mail detail**: an **Ask & Correct** button opens a live LLM chat over
  the selected mail — left chat history, right panel with the current
  urgency / summary / reason and the original body, bottom input sent by
  Enter or the Send button; the input and both buttons share one bordered row
  (equal height, no floating button above the input's first line). User and
  assistant messages are rendered as
  Markdown (the original mail remains escaped plain text). Each request runs
  in a cancellable Textual worker, so the modal remains responsive while the
  model is thinking; the send control is disabled until that response lands.
  The conversation is ephemeral (discarded on close, with a reminder in the
  header); the LLM can apply corrections to urgency / summary / reason (never
  the original body), which are persisted to the stored analysis and reflected
  in the right panel immediately. When a correction is applied the user's
  latest message is recorded into the rolling correction guidelines that every
  future LLM analysis receives (`feedback.guidelines`, most recent 20 kept),
  matching the old Reject behaviour.
- **Actions**: localized time / type / content / notes / source-mail columns;
  every stored time is displayed in `general.timezone`. **Add todo** creates a
  user-owned item; **Edit** opens the same form pre-filled for the row under
  the cursor (custom todos and imported seminar entries; mail-analysis items
  are source-owned and report that instead). Row selection still opens a
  detail modal that fills the screen — a scrollable box with the action plus
  the **source mail** (subject, sender, date, analysis summary/reason and the
  original body, HTML-as-text) and a close button pinned outside the scroll
  area. **Delete** removes selected custom todos and imported seminars for
  real; mail-analysis items are dismissed by their stable identity (mail id +
  due time + type) so re-analyzing the source mail keeps them hidden.
  Discovered seminar proposals are **not** advertised here as a feature: the
  review control appears (labelled with the pending count) only while
  proposals await confirmation, and opens the centered, scrollable review form
  with editable title, time window, timezone, location, URL, and description
  plus confidence/evidence. **Import to schedule** is an explicit
  confirmation, expired candidates require a future corrected start time, and
  Reject keeps a proposal hidden on later scans. Imported seminars preserve
  their source-mail link and use stable ids to prevent duplicate imports.
- **LLMs** (`settings.py: LLMPane`): the ordered fallback chain. Add / Edit /
  Delete plus Move up / Move down; the selection follows the moved entry so
  moves (and deletes) can be repeated, a rejected move puts the cursor back on
  the entry, and the ends of the chain report instead of jumping; the first row is the default and each row
  falls back to the ones below it, so `default` and `fallback` are never typed
  in by hand (the form hides them). The lower half of the tab is the
  **notification feed**: one colored entry per processed mail (all urgency
  levels, per-level colors, summary line), fed by the
  `mailflow.mail.processed` event. Raw LLM request logging lives in the main
  Logs tab — the router and backends log routing decisions and retries at
  INFO so the full LLM activity is visible there.
- **Runtime**: a plugins table (id/name/kinds/status) filling the pane
  height with quick Disable/Enable/Uninstall buttons, plus mail adapters,
  accounts (status/errors), LLMs, processor → LLM/fallback bindings and
  storage provider — all read from the service snapshot. **Double-clicking
  a plugin row opens the same plugin-detail dialog as the Market tab**;
  the dialog opens instantly from the market's cached entries when they
  are loaded, otherwise from the locally loaded plugin metadata (the
  module docstring becomes the readme) — no network fetch happens on the
  double-click path.
  Every pane loads asynchronously (mount workers) so startup never blocks.
- **Market**: VS Code-style plugin store — search input, localized category
  filter (known ids render through the language packs: `mail_source`,
  `processor`, `llm_backend`, `llm_enhancer`, `notifier`, `storage`,
  `bot_exporter`, **`gateway`**), a **sort dropdown** (name / status /
  category / installed-first / enabled-first / not-installed-first), a list
  of plugin name + description + version + status, and a detail pane
  rendering the plugin's markdown readme (scrollable; links are clickable and
  route through `general.browser_mode`) with Install / Uninstall / Enable /
  Disable buttons. **Locally installed and bundled plugins appear as entries
  too**, their docstrings becoming the detail readme, so chat providers can
  ship setup docs. The repository fetch runs in an exclusive worker and
  filtering renders from the cached entries. **New** opens the plugin wizard
  (`scaffold.py`); **Export** opens the bot-framework export wizard
  (`export.py`).
- **Export wizard** (`BotExportScreen`): framework `Select` (every
  registered `BOT_EXPORTER` plugin), `DirectoryTree` folder pick, optional
  subfolder checkbox + input, and a Generate button running
  `mailflow.bot_export.export_bot_plugin` in a worker.
- **Notifications** (`notifications.py: NotificationsPane`): manages *every*
  configured notifier — chat-platform gateways (NapCat/onebot, OpenWeChat,
  WeChatPadPro) and plain delivery channels (console, telegram, webhook, ntfy,
  smtp, ...). The table shows name / provider / enabled / urgency threshold /
  targets / live status. In-place actions toggle the selected notifier enabled
  and edit its delivery urgency; Add routes gateway-backed providers through
  the guided setup (`GatewayGuideModal`): auto-install/download or provision a
  per-instance Compose stack, start it, display real deployment progress, then
  drive **QR login inside the TUI**. A successful WeChatPadPro guide saves its
  actual endpoint as the notifier's `base_url`; a failed or cancelled guide
  saves no notifier. On mount the pane auto-connects every enabled notifier in
  a bounded worker and refreshes every 30s; failures render inline as
  `offline: <reason>`, never blocking startup. Deleting a gateway-backed row
  shuts the supervised process or Compose stack down first, so no orphaned
  platform instance remains running.
- **Settings**: the VS Code-style editor described below.
- **Logs**: a filterable viewer with a **bounded ring buffer (2000 lines)**,
  a level `Select` (WARNING+ERROR is the default, expandable to INFO/DEBUG),
  a source-group `Select` populated from the seen loggers, and a search box.
  Sources render as localized categories (`tui.logs_cat_*`: chat bot, mail,
  LLM, parsing, notify, storage, system) — every category the pane can derive
  has an entry in both packs, so a tag can never show a raw key.
  Rendering is **incremental**: each drain appends only newly pulled lines to
  the `RichLog` (capped via `max_lines`) instead of rebuilding the whole
  buffer every second — a full re-render happens only when a filter changes
  or lines fell off the ring buffer. This keeps the event loop responsive on
  slow terminals (headless containers, remote shells) even under heavy
  logging. The pane sits **after Settings** in the tab order.

Keyboard navigation: **Ctrl+1 … Ctrl+9** jump straight to a tab by its stable
id (mail, actions, mailboxes, LLMs, runtime, market, notifications, settings,
logs); hidden tabs in remote mode are skipped silently. These bindings are
`show=False` so they do not clutter the footer.

## Settings editor

`mailflow_tui/settings.py` is a client of `mailflow.settings`; it contains no
schema knowledge of its own.

- **Search box** (top) filters options across *every* section by key and
  description.
- **Sidebar** lists sections: MailFlow's own groups first, then one entry per
  plugin that owns configured components — installing a plugin makes its
  options appear under its own name.
- **Option cards** (right) show, per option: the dotted key (marked
  *modified* when it differs from the default), the localized description
  (`config.desc.<key>`, falling back to the pydantic field description), the
  default value, an editor chosen from `EditorKind`, and **Save** +
  **Restore default** buttons.
- List and mapping values open `ListEditScreen` (one entry per line, or JSON
  for mappings); structured entries open `EntryFormScreen`, a real form window
  with per-field labels, descriptions and a Back button.
- Editing an `[[llms]]` entry (including renaming its `llm_id`) succeeds: the
  settings editor re-derives `default`/`fallback` from the list order and
  rewrites every processor binding *before* validating, so a valid edit is not
  rejected for a reference it is about to update. Deleting an LLM clears the
  bindings that named it instead of leaving them dangling.
- **Plugin-declared forms**: a plugin may register `FormField`s for a
  component (`registrar.add_form_fields(kind, component_id, fields)`); the
  form renders them generically (string / password / number / boolean /
  list / select / textarea), falling back to the built-in field layouts when
  a provider declares none. An optional plugin `probe` backs the Test button
  and the Notifications status column. The contract is capability-based — a
  `mail_source` plugin may declare exactly the fields its transport needs
  (it could connect to a message platform that is only "like a mailbox").
- LLM entries edit their **timeout** (and retry count) directly in the form:
  a slow or cold model needs the wait for its first token plus the whole
  answer, and a config written by an earlier version has its legacy 60 s/30 s
  defaults raised automatically (see `mailflow.config.migrate_legacy_timeouts`;
  a value the user chose deliberately is preserved).
- Each form's **Test** button dispatches by group: mailbox forms run a
  real IMAP login probe (20s socket timeout), LLM forms send a one-shot
  completion (60s request budget, 120s wait, so a cold local model's
  first-token latency does not read as a failure) and report latency plus the
  model name, notifier forms probe the registered connector.
- An invalid edit shows which option is wrong and why (from
  `SettingsError.option`/`.message`) in the status line and as a notification;
  a valid edit is persisted immediately through the service.
- Adding, editing, deleting or reordering a list entry returns immediately:
  the runtime keeps its live source tasks (and their connections) for any edit
  that cannot have changed them — an LLM timeout, a notifier target, a
  reordered chain — and only restarts them when the accounts, their options or
  an adapter's class actually changed (plugin loads force a restart). Restarting
  unconditionally made those edits wait for a source to be torn down, which a
  blocking connect can hold for seconds.
- The language `Select` lives in this tab and persists through
  `service.set_language`.

## Reply modal

`TextArea` with placeholder; separate **Save / Prepare / Confirm / Cancel**
buttons. Confirm is disabled until Prepare issues the token; after a
successful confirm the status shows "sent" and Confirm disables again.

## Buttons

Every button carries an explicit `variant`, and `app.tcss` gives the base
`Button` rule an opaque background: Textual's stock theme draws unstyled
variants on `ansi_default`, which renders black-on-black in dark terminals.
Modal dialogs always offer a visible Back/Cancel control — escape is a
shortcut, never the only way out.

## i18n

All user-facing labels come from `service.t(...)`, including stored urgency and
action-type values, localized setting defaults, validation feedback and empty
states. A language change re-renders screens through the `language.changed`
event. Panes that are already composed are relabeled in a worker guarded by a
lock.
Generic backend failures use the active-language `common.error` framing. Their
diagnostic detail is escaped before Rich rendering and redacts configured
credentials (including secret notifier options and LLM headers); external
transport text itself remains diagnostic data rather than a second UI language.
Language remounting re-checks each tab container after asynchronous removal, so
closing the app during a language switch cannot raise a mount failure.

Before a remote service is connected, its login form uses the locally saved
language pack from the last successful session. After authentication,
`RemoteServiceAdapter` adopts `/snapshot.language` before the main app mounts
and follows relayed `language.changed` events, so the remote UI cannot remain
in a stale local language.

## Live updates

The app subscribes to `mailflow.mail.processed` — the name the runtime
actually emits — and reloads the Mail, Actions and Runtime panes when a mail
finishes processing.

## Verification

Headless tests drive the app with Textual's `run_test` pilot: compose, mail
table population, timezone-projected action timestamps, search filtering,
urgency mutation through the Select, language persistence and localized select
options, prepare/confirm gating of the reply modal, the settings cards (save /
invalid value / restore default), the LLM chain reordering, the mailbox history
browser (analyze a picked mail, skip a known one), the repository dialog's Back
button, the Notifications pane (lists all notifiers, toggles enabled, edits
urgency), the todo create/edit round trip through the Actions table, the smart
action (real progress, ranked matches, a scheduled seminar entry with its
source-mail link and event window, and no review control when nothing is
pending), and that a processed-mail event refreshes the panes without a manual
refresh.

## Opening web links (browser_mode)

`general.browser_mode` controls how the TUI opens external web links
(plugin homepage, documentation links in readmes):

- `system` (default): the system browser via `webbrowser` — works on
  desktop hosts; on headless servers this silently does nothing visible.
- `graphical`: renders the page inside the terminal through a
  **Carbonyl-compatible rendering service**; set `general.browser_render_url`
  to its base URL (e.g. `http://127.0.0.1:8080`). The service renders the
  page server-side and streams a terminal-compatible representation
  (Sixel / Kitty graphics or ANSI text) to the TUI.
- `disabled`: link clicks show an explanatory status instead of opening.

### Running a Carbonyl render service

For headless hosts (PVE LXC containers, servers) run Carbonyl as a
service that the TUI calls:

```bash
# example: dockerized carbonyl (graphical render endpoint)
docker run -d -p 8080:8080 --name carbonyl \
  fathyb/carbonyl --chromium-arg=--no-sandbox
```

Then set in the config:

```toml
[general]
browser_mode = "graphical"
browser_render_url = "http://127.0.0.1:8080"
```

Notes:

- The terminal must support the image protocol Carbonyl emits (Kitty
  graphics or Sixel); otherwise fall back to `system` mode.
- Any service implementing Carbonyl's render contract (`GET {render}/{url}`
  returning terminal-renderable output) can be used in place of Carbonyl.
- `browser_mode` changes apply on restart.

## Remote mode and embedded server

`mailflow tui --local` starts the TUI together with the embedded
admin REST+WS server (`mailflow_server.create_app`, credentials in
`[server]`, auto-provisioned for the session). Other frontends —
another TUI, a chat bot — attach with `mailflow tui --remote URL`:
the login screen remembers address/username in
`~/.mailflow/tui-session.json`, optionally stores the password and
auto-logins until authentication fails. Remote sessions drive mail,
actions, runtime toggles, logs (websocket relay) and scalar settings;
mailbox history browsing, LLM chain editing and marketplace installs
require a locally attached service.