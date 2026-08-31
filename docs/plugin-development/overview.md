# Plugin development overview

A MailFlow plugin is a Python distribution that exposes a `plugin` singleton
implementing two Pluggy hooks (group `mailflow.plugins`). It can provide one
or more components: mail sources, LLM backends, processors, notifiers or a
storage backend.

## Anatomy

```
plugins/mailflow-mail-fake/
├── pyproject.toml
└── src/mailflow_mail_fake/
    ├── __init__.py       # re-exports `plugin`
    └── plugin.py         # PluginInfo + register hooks
```

```toml
[project]
name = "mailflow-mail-fake"
version = "0.1.0"
dependencies = ["mailflow-core"]

[project.entry-points."mailflow.plugins"]
fake-mail = "mailflow_mail_fake.plugin:plugin"
```

## Hooks

**Declarative style (recommended)** — one definition plus one decorated
class per component; ids resolve from the decorator argument,
`component_id`/`backend_id`/`processor_id` attributes, or the class name:

```python
from mailflow.plugin_api import define_plugin

PLUGIN = define_plugin(
    "mailflow-mail-fake",  # unique across all plugins
    name="Fake Mail Source",
    version="0.1.0",
    description="...",
)


@PLUGIN.source("fake")
class FakeSource:
    async def run(self, emit, stop_event) -> None: ...


plugin = PLUGIN.build()
```

Available decorators: `@PLUGIN.source`, `@PLUGIN.processor`, `@PLUGIN.llm`,
`@PLUGIN.notifier`, `@PLUGIN.storage`, `@PLUGIN.bot_exporter` — the
component kinds are collected automatically. The scaffold templates
generate this style, so a new plugin is a definition plus one class.

**Classic style** — the two hooks by hand, equivalent to the declarative
API:

```python
from mailflow.plugins import PluginInfo

PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-mail-fake",
    name="Fake Mail Source",
    version="0.1.0",
    description="...",
    kinds=[ComponentKind.MAIL_SOURCE],
)


class MyPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar, config) -> None:
        registrar.add_source("fake", build_source)  # see per-kind guides


plugin = MyPlugin()
```

`plugin_id` must be unique — a duplicate registration is rejected. A
broken `info`/`register` hook is isolated: the plugin is skipped and startup
continues (the failure is logged).

## Rules

- Depend only on `mailflow-core` (or other plugin packages); never import
  hosts. Core never imports your plugin.
- Register **component ids** (e.g. `fake`, `sqlite`, `openai-compatible`),
  not package names — configuration references component ids.
- Declare `kinds` so snapshots and `plugin list` show what you provide.
- Never log credentials; sanitize error text before it can reach persisted
  notes.
## Declaring forms and probes

Fields and probes are registered through the classic registrar inside
`mailflow_register` (the declarative `PluginBuilder` currently covers
component factories only):

```python
from mailflow.forms import FormField


def mailflow_register(self, registrar, config) -> None:
    registrar.add_source("imap", ImapSource)
    registrar.add_form_fields(
        ComponentKind.MAIL_SOURCE,
        "imap",
        [
            FormField(field_id="host", kind="string", default="imap.example.com", required=True),
            FormField(field_id="use_tls", kind="boolean", default=True),
        ],
    )
```

`registrar.add_form_fields(kind, component_id, fields)` is
`PluginRegistrar.add_form_fields`; the runtime snapshot reads them back via
`ComponentRegistry.form_fields(kind, component_id)`. `FormField.kind` is
one of the `Literal` strings `string | password | number | boolean | list |
select | textarea` (the `FormFieldKind` type alias in `mailflow.forms`).
`label_key`/`description_key` carry locale keys and fall back to the
built-in `tui.extras_<id>` labels.

**Probes** verify a configured component live — they back the form's Test
button and the Notifications tab status column:

```python
async def probe_imap(config: dict, instance: ImapSource) -> str:
    return "logged-in-as user"  # or "offline: connection refused"


registrar.add_probe(ComponentKind.MAIL_SOURCE, "imap", probe_imap)
```

`ProbeFn = Callable[[dict[str, Any], Any], Awaitable[str]]`: the raw config
mapping plus the instantiated component; return a human-readable status
string. Components without a probe report `"not probed"`.

The contract is **capability-based, not literal-type-based**: a
`mail_source` plugin may connect to anything "like a mailbox" (IMAP, a
chat platform, an API) and declare exactly the fields its transport needs.
The marketplace validates a plugin by loading it, instantiating every
registered factory and probing — it never assumes a known component-id
vocabulary.

## Gateway provisioners

`gateway` is both a marketplace category and a component kind
(`ComponentKind.GATEWAY_PROVISIONER`). A gateway plugin installs / starts /
supervises an external bot runtime (NapCat, WeChaty, OpenWeChat) and drives
its QR login from inside the TUI; the `NotificationsPane` routes
gateway-backed notifiers through it. Author one with the `gateway`
scaffold (`mailflow plugin new --kind gateway`) or the declarative
decorator:

```python
from typing import Any

from mailflow.contracts import GatewayInstance, GatewayProvisioner


@PLUGIN.gateway_provisioner("napcat")
class NapCatProvisioner(GatewayProvisioner):
    async def detect(self) -> str: ...
    async def install(self, instance_id: str, options: dict[str, Any]) -> None: ...
    async def start(self, instance_id: str, options: dict[str, Any]) -> GatewayInstance: ...
    async def stop(self, instance_id: str) -> None: ...
    async def status(self, instance_id: str) -> GatewayInstance: ...
    async def qr(self, instance_id: str) -> str: ...
```

The factory registered by the decorator is the zero-argument callable that
returns the provisioner object; `mailflow.gateway.GatewayManager` drives the
lifecycle (persist, restart on crash, stop on shutdown).

See `docs/architecture/tui-notifications-and-plugin-ecosystem.md` §1 for
the guided-setup flow.

## Guides

- `mail-source.md` — emit normalized mails, send replies
- `processor.md` — classify/filter mails in the chain
- `llm-backend.md` — a chat-completions transport
- `notifier.md` — deliver computed analyses
- `storage.md` — durable persistence with trash semantics
- `bot-exporter.md` — turn a configured instance into a chatbot-framework plugin

See [development/deployment.md](../development/deployment.md) for the
three-platform setup (Windows/Linux/macOS): running the project, exporting
bot plugins, installing plugins, and managing a service from CLI/TUI/chat.

## Testing

Register your plugin through the normal hooks in tests and drive it via
`start_service` (see `tests/e2e/test_start_service.py`), or test the class
directly with the `mailflow-testkit` fakes (`make_mail`, `FakeLLMBackend`).
