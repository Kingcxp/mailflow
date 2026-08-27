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

## Tabs

- **Mail**: search `Input` with placeholder; urgency-colored `DataTable`
  (■ + value in the contract color) that fills the pane height; the scrollable
  detail pane shows summary, reason, action items, the **original body**
  (HTML rendered as text, binary attachment payloads detected and replaced
  by an explanatory note, bodies truncated at 4000 chars), attachment
  metadata (name/type/size) and failed processor notes. The bottom control
  row holds the three selects (manual urgency — a localized
  `ad/info/important/urgent/follow-automatic` dropdown that mirrors the
  selected mail — plus urgency filter and sort) and a two-row button
  container (refresh/trash/feedback then reply/re-analyze/re-analyze-failed)
  with equal-width buttons. Reply opens the confirmation-gated modal.
- **Mailboxes** (`settings.py: AccountsPane`): accounts table with
  Add / Edit / Delete (forms, not TOML editing) plus the **history browser** —
  Load history pages a mailbox newest-first through
  `service.fetch_history(account_id, limit=, offset=)`, rows are toggled with
  Enter/click, and *Analyze selected* runs only the picked mails through
  `service.process_mail`. Already-stored mail is marked and skipped, so
  re-analyzing is a no-op instead of a duplicate. Sources that do not
  implement the optional history capability report that instead of failing.
- **Mail detail**: a **Reject** button opens a dialog to record a reason;
  the reason joins the rolling correction guidelines that every future LLM
  analysis receives (`feedback.guidelines`, most recent 20 kept), and the
  detail view marks the mail with its rejection reason.
- **Actions**: time / type / content / notes / source-mail columns; row
  selection opens a detail modal that fills the screen — a scrollable box
  with the action plus the **source mail** (subject, sender, date, analysis
  summary/reason and the original body, HTML-as-text) and a close button
  pinned outside the scroll area. **Delete** removes the selected entry:
  user-created todos are deleted for real, mail-derived ones are dismissed
  by their stable identity (mail id + due time + type) so re-analyzing the
  source mail keeps them hidden.
- **LLMs** (`settings.py: LLMPane`): the ordered fallback chain. Add / Edit /
  Delete plus Move up / Move down; the first row is the default and each row
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
  a plugin row opens the same plugin-detail dialog as the Market tab.**
  Every pane loads asynchronously (mount workers) so startup never blocks.
- **Market**: VS Code-style plugin store — search input, localized category
  filter (known ids render through the language packs), a **sort dropdown**
  (name / status / category / installed-first / enabled-first /
  not-installed-first), a list of plugin name + description + version +
  status, and a detail pane rendering the plugin's markdown readme
  (scrollable; links are clickable and route through `general.browser_mode`)
  with Install / Uninstall / Enable / Disable buttons. **Locally installed
  and bundled plugins appear as entries too**, their docstrings becoming the
  detail readme, so chat providers can ship setup docs. The repository fetch
  runs in an exclusive worker and filtering renders from the cached entries.
  **New** opens the plugin wizard (`scaffold.py`); **Export** opens the
  bot-framework export wizard (`export.py`).
- **Export wizard** (`BotExportScreen`): framework `Select` (every
  registered `BOT_EXPORTER` plugin), `DirectoryTree` folder pick, optional
  subfolder checkbox + input, and a Generate button running
  `mailflow.bot_export.export_bot_plugin` in a worker.
- **Logs**: `RichLog` fed by the injected handler on a timer — never stdout
  scraping.
- **Settings**: the VS Code-style editor described below.

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
- Each form's **Test** button dispatches by group: mailbox forms run a
  real IMAP login probe (20s socket timeout), LLM forms send a one-shot
  completion and report latency plus the model name.
- An invalid edit shows which option is wrong and why (from
  `SettingsError.option`/`.message`) in the status line and as a notification;
  a valid edit is persisted immediately through the service.
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

All labels come from `service.t(...)`; a language change re-renders the
screens through the `language.changed` event. Panes that are already composed
are relabeled in a worker guarded by a lock.

## Live updates

The app subscribes to `mailflow.mail.processed` — the name the runtime
actually emits — and reloads the Mail, Actions and Runtime panes when a mail
finishes processing.

## Verification

Headless tests drive the app with Textual's `run_test` pilot: compose, mail
table population, search filtering, urgency mutation through the Select,
language persistence, prepare/confirm gating of the reply modal, the settings
cards (save / invalid value / restore default), the LLM chain reordering, the
mailbox history browser (analyze a picked mail, skip a known one), the
repository dialog's Back button, and that a processed-mail event refreshes the
panes without a manual refresh.

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

## Bots tab

The Bots tab lists configured onebot/wechaty/openclaw-weixin notifier
instances (table fills the tab height) and probes their login state on
demand with **concurrent** checks run in a worker. QR scanning happens in
the bot runtime itself (NapCat, WeChaty via the **pad protocol** — see the
plugin's market doc, OpenClaw); the tab verifies the session MailFlow sends
through. Probe statuses are localized (logged-in-as / online / unreachable /
HTTP status). The add form is a real
`EntryFormScreen`: the provider is a dropdown limited to the IM providers,
each provider renders its own option fields (endpoint URL, token, targets)
with bilingual descriptions, and a help line above the table explains the
external-runtime login model.
