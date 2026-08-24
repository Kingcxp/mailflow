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
  (■ + value in the contract color); detail pane with summary, reason,
  action items, **original body** and processor notes; urgency `Select`
  with a one-line help tooltip (`ad`/`info`/`important`/`urgent`/`auto`);
  Refresh / Trash / Reply buttons. Reply opens the confirmation-gated modal.
- **Mailboxes** (`settings.py: AccountsPane`): accounts table with
  Add / Edit / Delete (forms, not TOML editing) plus the **history browser** —
  Load history pages a mailbox newest-first through
  `service.fetch_history(account_id, limit=, offset=)`, rows are toggled with
  Enter/click, and *Analyze selected* runs only the picked mails through
  `service.process_mail`. Already-stored mail is marked and skipped, so
  re-analyzing is a no-op instead of a duplicate. Sources that do not
  implement the optional history capability report that instead of failing.
- **Actions**: time / type / content / notes / source-mail columns; row
  selection opens a detail modal; **Delete** removes the selected entry.
  User-created todos are deleted for real; mail-derived todos are dismissed
  by their stable identity (mail id + due time + type), so re-analyzing the
  source mail keeps them hidden instead of resurrecting them.
- **LLMs** (`settings.py: LLMPane`): the ordered fallback chain. Add / Edit /
  Delete plus Move up / Move down; the first row is the default and each row
  falls back to the ones below it, so `default` and `fallback` are never typed
  in by hand (the form hides them). The lower half of the tab is the
  **notification feed**: one colored entry per processed mail (all urgency
  levels, per-level colors, summary line), fed by the
  `mailflow.mail.processed` event. Raw LLM request logging lives in the main
  Logs tab — the router and backends log routing decisions and retries at
  INFO so the full LLM activity is visible there.
- **Runtime**: a plugins table (id/name/kinds/status) with quick
  Disable/Enable buttons (persisted, applies on next start), plus mail
  adapters, accounts (status/errors), LLMs, processor → LLM/fallback
  bindings and storage provider — all read from the service snapshot.
- **Market**: VS Code-style plugin store — search input, category filter,
  a list of plugin name + description + version + status, and a detail pane
  rendering the plugin's markdown readme with Install / Uninstall / Enable /
  Disable buttons acting on the selected plugin. The repository fetch runs in
  an exclusive worker, and search/category filtering renders from the cached
  entries, so the UI stays interactive and typing never triggers a network
  round-trip per keystroke. **New** opens the plugin wizard (`scaffold.py`);
  **Export** opens the bot-framework export wizard (`export.py`).
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
instances and probes their login state on demand. QR scanning happens
in the bot runtime itself (NapCat, WeChaty gateway, OpenClaw); the tab
verifies the session MailFlow sends through. The add form is a real
`EntryFormScreen`: the provider is a dropdown limited to the IM providers,
each provider renders its own option fields (endpoint URL, token, targets)
with bilingual descriptions, and a help line above the table explains the
external-runtime login model.
