# Textual TUI

`packages/mailflow-tui` is a Textual client of the Core service — it renders
service data and calls service methods; no business logic lives in the UI.

## Runner

`mailflow_tui/runner.py` builds the bundled plugin manager, starts one
service with an injected `TuiLogHandler` (records into a queue), runs the
Textual app on the same event loop, and stops the service in `finally`.

## Tabs

- **Mail**: search `Input` with placeholder; urgency-colored `DataTable`
  (■ + value in the contract color); detail pane with summary, reason,
  action items, **original body** and processor notes; urgency `Select`
  with a one-line help tooltip (`ad`/`info`/`important`/`urgent`/`auto`);
  Refresh / Trash / Reply buttons. Reply opens the confirmation-gated modal.
- **Actions**: time / type / content / notes / source-mail columns; row
  selection opens a detail modal.
- **Runtime**: a plugins table (id/name/kinds/status) with quick
  Disable/Enable buttons (persisted, applies on next start), plus mail
  adapters, accounts (status/errors), LLMs, processor → LLM/fallback
  bindings and storage provider — all read from the service snapshot.
- **Market**: VS Code-style plugin store — search input, category filter,
  a list of plugin name + description + version + status, and a detail pane
  rendering the plugin's markdown readme with Install / Uninstall / Enable /
  Disable buttons acting on the selected plugin.
- **Logs**: `RichLog` fed by the injected handler on a timer — never stdout
  scraping.
- **Settings**: language `Select` with an explanation; switching persists
  through `service.set_language` (storage-backed).

## Reply modal

`TextArea` with placeholder; separate **Save / Prepare / Confirm / Cancel**
buttons. Confirm is disabled until Prepare issues the token; after a
successful confirm the status shows "sent" and Confirm disables again.

## i18n

All labels come from `service.t(...)`; a language change re-renders the
screens through the `language.changed` event.

## Verification

Headless tests drive the app with Textual's `run_test` pilot: compose, mail
table population, search filtering, urgency mutation through the Select,
language persistence, and the prepare/confirm gating of the reply modal.
