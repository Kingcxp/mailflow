# Embedding MailFlow

MailFlow is designed to be embedded: a chat-bot framework, a service daemon
or any async application can host the whole mail pipeline with one call.

## One method to start everything

```python
import asyncio, logging
from mailflow.service import start_service
from mailflow_bundled import create_plugin_manager


class MyLogHandler(logging.Handler):
    def emit(self, record): ...  # forward records to your framework


async def main() -> None:
    service = await start_service(
        config,  # MailFlowConfig or leave None for defaults
        plugin_manager=create_plugin_manager(config),  # bundled set + discovery
        extra_log_handlers=[MyLogHandler()],  # optional host log sink
    )
    # ... use the service ...
    await service.stop()
```

`start_service` composes configuration, logging (queue sinks with secret
redaction), plugin discovery, storage, LLM backends, processors, sources,
notifiers, the event bus and the runtime. `run_service(...)` is a standalone
wrapper around `asyncio.run` for non-embedding use.

## What a host gets

`MailFlowService` is the whole public surface (58 methods). The groups a host
usually needs:

**Inspection**

- `snapshot()` — plugins (id/name/version/kinds/components), mail adapters,
  accounts with status/errors, loaded LLMs with their backend plugin,
  processor→LLM/fallback bindings, language, timezone, storage, version.
- `list_mails(limit=None)` / `get_mail(id)` / `count_mails()` /
  `list_actions()` / `list_trash()` — full records with analysis, action items
  and the original body.

**Mail and action mutations**

- `set_mail_urgency(id, urgency|None)` (None resets to the automatic value),
  `delete_mail(id)` (to trash), `restore_mail(id)`, `run_cleanup()`.
- `add_action(summary, due_at, action_type=, notes=)`, `delete_action(id)`.
- `record_feedback(mail_id, reason)` / `get_feedback` / `feedback_guidelines`
  — user notes roll into the guidelines injected into every LLM analysis.

**Mailbox history (browse and analyze existing mail)**

- `history_accounts()` — account ids whose source implements the optional
  `HistoryCapableSource` capability.
- `fetch_history(account_id, limit=50, offset=0)` — already-received mail,
  newest first. Nothing is stored or analyzed; raises `KeyError` for an
  unknown account and `NotImplementedError` for a source without the
  capability.
- `is_mail_known(mail)` — whether that mail is already stored.
- `process_mail(mail)` — run one mail through the pipeline now, exactly like
  live mail (dedup, persistence retry, `mailflow.mail.processed`, notifier
  thresholds). Returns the stored record, or `None` when it was a duplicate.

**Configuration and settings**

- `settings_sections()` / `settings_option(key)` — the editor model from
  `mailflow.settings` (sections per plugin, editor kind, default, current
  value); see `docs/configuration/overview.md`.
- `set_setting(key, value)` / `reset_setting(key)` — coerce, validate and
  persist; raise `SettingsError` naming the offending option.
- `add_config_entry(group, values)` / `update_config_entry(group, index,
  values)` / `remove_config_entry(group, index)` / `move_config_entry(group,
  index, offset)` for `accounts`/`llms`/`processors`/`notifiers`.
- `list_config_options()` / `get_config_option(key)` / `set_config_value(key,
  raw)` — the flat view used by the `config` command.
- Persisting needs a config file: pass `config_path=` to `start_service`, or
  every mutation raises `ValueError("no config file loaded")`.

**Plugins and updates**

- `plugin_status(id)`, `plugin_enable(id)`, `plugin_disable(id)` (applies on
  next start), `plugin_uninstall(id)`, `plugin_repo_add/remove(name, url)`.
- `check_updates()`, `apply_updates()`, `installed_plugin_versions()`.
- `service.market` — the `PluginMarket` client (blocking; call it in a
  thread/worker).

**Replies and i18n**

- `create_reply(mail_id)` → `edit_draft` → `prepare_reply` (issues a
  short-lived token) → `confirm_reply(draft_id, token)`; `cancel_reply`,
  `get_draft`, `create_letter_draft(mail_id, "cn"|"en", ...)`.
- `t(key, **params)`, `get_language()`, `set_language(code)` (persisted),
  `available_languages()`.

**Commands and events**

- `service.commands.execute("mail list")` — the shared command router returns
  a transport-neutral `CommandResponse` (plain text + style spans), so chat
  platforms render the same management commands as the CLI. Top-level
  commands: `help`, `mail`, `action`, `plugin`, `adapter`, `account`, `llm`,
  `reply`, `lang`, `trash`, `runtime`, `config`, `feedback`, `update`.
  Construct it once with `CommandRouter(service)` (it wires itself onto
  `service.commands`).
- `service.on(event, handler)` returns an unsubscribe callable. Handlers are
  awaited concurrently; one failing handler is logged and never blocks the
  others.

| Event | Emitted by | Payload |
| ----- | ---------- | ------- |
| `mailflow.mail.received` | runtime | `mail` |
| `mailflow.mail.processed` | runtime | `record` |
| `mailflow.account.error` | runtime | `account_id`, `error` |
| `mailflow.cleanup.done` | runtime | `moved`, `purged` |
| `mailflow.action.reminder` | runtime | `item`, `record`, `kind`, `scheduled` |
| `mailflow.action.digest` | runtime | `date`, `today_count`, `upcoming_count`, `items` |
| `mailflow.runtime.stopping` | runtime | — |
| `mailflow.update.checked` / `.applied` | service | version/report fields |
| `mail.deleted` / `mail.urgency.changed` | service | `record_id` (+ `urgency`) |
| `reply.created` / `reply.sent` | service | `draft_id`, `mail_id` |
| `language.changed` | service | `language` |
| `config.changed` | service | `key` |
| `plugin.enabled` / `plugin.disabled` | service | `plugin_id` |

Handlers receive the event name as `event=` plus the payload as keyword
arguments: `async def handler(event: str, **payload) -> None`.

## Host contract

- Core never reconfigures the root logger and never calls `basicConfig()`;
  your framework's logging is untouched. Add your handlers via
  `extra_log_handlers`.
- Keep your host on the same event loop: `start_service` is async and the
  runtime's tasks belong to the loop that called it.
- Stop politely with `await service.stop()` (graceful task cancellation,
  source close, storage close).
