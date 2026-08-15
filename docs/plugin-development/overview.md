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

```python
from mailflow.plugins import PluginInfo

PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-mail-fake",   # unique across all plugins
    name="Fake Mail Source",
    version="0.1.0",
    description="...",
    kinds=[ComponentKind.MAIL_SOURCE],
)

class MyPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar, config) -> None:
        registrar.add_source("fake", build_source)   # see per-kind guides

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
- Keep processors deterministic where possible and fast; LLM work goes
  through the injected `LLMRouter`.

## Guides

- `mail-source.md` — emit normalized mails, send replies
- `processor.md` — classify/filter mails in the chain
- `llm-backend.md` — a chat-completions transport
- `notifier.md` — deliver computed analyses
- `storage.md` — durable persistence with trash semantics

## Testing

Register your plugin through the normal hooks in tests and drive it via
`start_service` (see `tests/e2e/test_start_service.py`), or test the class
directly with the `mailflow-testkit` fakes (`make_mail`, `FakeLLMBackend`).
