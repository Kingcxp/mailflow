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
        config,                       # MailFlowConfig or leave None for defaults
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

- `service.snapshot()` — plugins (id/name/version/kinds/components), mail
  adapters, accounts with status/errors, loaded LLMs with their backend
  plugin, processor→LLM/fallback bindings, language, timezone, storage.
- `service.list_mails()` / `get_mail(id)` / `count_mails()` / `list_actions()`
  / `list_trash()` — full records with analysis, action items and original
  body.
- `service.set_mail_urgency(id, urgency|None)`, `delete_mail`, `restore_mail`,
  `set_language(code)` (persisted).
- `service.commands.execute("mail list")` — the shared command router returns
  transport-neutral `CommandResponse` (plain text + style spans), so chat
  platforms can render the same management commands as the CLI.
- `service.on("mail.processed", handler)` — async event subscription
  (also `mail.received`, `mail.deleted`, `reply.sent`, `cleanup.done`,
  `language.changed`, ...).
- The confirmed reply workflow (`create_reply` → `prepare_reply` → token →
  `confirm_reply`).

## Host contract

- Core never reconfigures the root logger and never calls `basicConfig()`;
  your framework's logging is untouched. Add your handlers via
  `extra_log_handlers`.
- Keep your host on the same event loop: `start_service` is async and the
  runtime's tasks belong to the loop that called it.
- Stop politely with `await service.stop()` (graceful task cancellation,
  source close, storage close).
