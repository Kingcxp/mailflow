# Embedding

Use the same service entry point as the bundled hosts. Supply the bundled plugin
manager when those components are needed, keep it on one event loop, and always
await shutdown.

```python
import asyncio

from mailflow.commands import CommandRouter
from mailflow.config import load_config
from mailflow.permissions import PermissionContext
from mailflow.service import start_service
from mailflow_bundled import create_plugin_manager


async def main() -> None:
    path = "configs/local.toml"
    config = load_config(path)
    service = await start_service(
        config,
        config_path=path,
        plugin_manager=create_plugin_manager(config),
    )
    try:
        router = CommandRouter(service)
        # Resolve identity and administrator status in the host.
        context = PermissionContext(sender="reader", provider="my-host")
        response = await router.execute("mail list", context=context)
        print(response.text)
    finally:
        await service.stop()


asyncio.run(main())
```

A supplied plugin manager owns discovery; `create_plugin_manager` registers the
bundled set and discovers external entry points. Bare `mailflow-core` hosts can
provide their own manager and components. Do not import TUI or CLI internals.

## Service and permissions

[MailFlowService](../../packages/mailflow-core/src/mailflow/service.py) provides
mail/history access, analysis, tasks, reply drafts, settings, plugins and runtime
status. Use those methods rather than mutating storage or runtime internals.
Persistent settings need `config_path`; remote operations must match the server's
explicit supported methods.

Every external request needs a
[PermissionContext](../../packages/mailflow-core/src/mailflow/permissions.py)
with sender, provider, instance and chat IDs where applicable. Resolve
`is_admin` from the host's trusted configuration. Pass it to `CommandRouter`,
or wrap direct service calls in `permission_scope(context)`. The default
in-process context is trusted local administration and must not be inherited
by an external request.

Use `service.t(key, **params)` for user messages and translate exceptions through
the shared error component. Return the shared structured command response so
adapters can render equivalent results without rebuilding command logic.

## Events and logging

```python
async def on_processed(event: str, **payload) -> None:
    record = payload["record"]
    print(record.mail.subject)


unsubscribe = service.on("mailflow.mail.processed", on_processed)
# Call unsubscribe() when the host no longer needs the subscription.
```

Runtime events include `mailflow.mail.received`, `mailflow.mail.processed`,
`mailflow.account.error`, `mailflow.cleanup.done` and `mailflow.action.reminder`.
Service events include `mail.deleted`, `mail.urgency.changed`, `reply.created`,
`reply.sent`, `language.changed` and `config.changed`. Handlers receive `event=`
and payload keyword arguments. REST/WebSocket hosts convert these into the
shared remote event frame rather than inventing another payload shape.

Pass host log handlers via `extra_log_handlers` when starting the service.
Core does not reconfigure the root logger. Avoid logging request credentials,
raw configuration or secret-bearing transport headers.

## Ownership

The service owns runtime tasks, storage, gateways and bridges. Let its reload
path replace components and stop the previous ones. Stop the service before
closing the host event loop; do not start a second runtime over the same service
or independently close resources it still owns.

See [architecture](../architecture/overview.md) for data rules and
[plugin development](../plugin-development/overview.md) for component interfaces.
