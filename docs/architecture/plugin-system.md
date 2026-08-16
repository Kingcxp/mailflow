# Plugin system

Plugins are the unit of extension: mail sources, LLM backends, processors,
notifiers and storage backends. Discovery uses Pluggy; ownership and
composition use Core's own registry.

## Hooks

Every plugin module exposes a singleton `plugin` implementing two hooks
(group `mailflow.plugins`):

- `mailflow_plugin_info() -> PluginInfo` — id, name, version, description,
  kinds (which component categories it provides).
- `mailflow_register(registrar, config) -> None` — registers component
  factories with the `PluginRegistrar`.

```python
from mailflow.plugins import PluginInfo

PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-mail-fake",
    name="Fake Mail Source",
    version="0.1.0",
    description="...",
    kinds=[ComponentKind.MAIL_SOURCE],
)


class FakeMailPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar, config) -> None:
        registrar.add_source("fake", build_source)


plugin = FakeMailPlugin()
```

## Entry points

Installed distributions declare `[project.entry-points."mailflow.plugins"]`
(e.g. `fake-mail = "mailflow_mail_fake.plugin:plugin"`). `PluginManager`
discovers them with `importlib.metadata.entry_points().select(group=...)`;
a plugin whose hooks raise is logged and skipped — one broken plugin cannot
kill startup.

## Allow/deny and lifecycle

`[plugins] enabled = [...]` (non-empty acts as an allowlist) and
`disabled = [...]` filter plugins by id at registry build time. `plugin
enable/disable <id>` mutate these lists and persist the config (applies on
the next start, like VS Code's restart requirement). Enabling/disabling is
safe: `start_service` skips config entries whose component plugin is not
loaded (accounts/LLMs/processors/notifiers) with a warning instead of
crashing. `plugin uninstall <id>` removes the pip package (marketplace
plugins only); bundled plugins are part of the distribution and are only
enabled/disabled, never uninstalled.

## Registry and ownership

`ComponentRegistry` stores typed factories per `ComponentKind` plus a
snapshot of `(component_id, kind, plugin_id)`. `PluginRegistrar` stamps the
plugin id **at registration time** — ownership is a fact, never a runtime
search for "the first plugin with capability X". Duplicate component ids
are rejected.

Typed accessors (`source_factory`, `llm_factory`, `processor_factory`,
`notifier_factory`, `storage_factory`) keep factory signatures checkable by
mypy/pyright.

## Bundled composition and frozen builds

`mailflow-bundled` imports the eight official plugin singletons and registers
them **before** optional entry-point discovery. Static imports make frozen
(Nuitka) executables independent of entry-point metadata; discovery dedupes
by plugin id, so the same plugins are not registered twice in development.
Frozen mode does not promise arbitrary post-build plugin discovery.

## Component kinds

| Kind              | Factory signature                                  | Component id examples |
| ----------------- | -------------------------------------------------- | --------------------- |
| `MAIL_SOURCE`     | `(MailAccountConfig) -> MailSource`                | `fake`                |
| `LLM_BACKEND`     | `(LLMConfig) -> LLMBackend`                        | `openai-compatible`   |
| `MAIL_PROCESSOR`  | `(ProcessorConfig, LLMRouter) -> MailProcessor`    | `rules`, `llm-importance` (built into core) |
| `LLM_ENHANCER`    | `(ProcessorConfig) -> LLMEnhancer`                 | `my-enhancer`         |
| `NOTIFIER`        | `(NotifierConfig) -> Notifier`                     | `console`             |
| `STORAGE`         | `(StorageConfig) -> StorageBackend`                | `sqlite`              |
| `BOT_EXPORTER`    | `(BotExportContext) -> BotExportResult`            | `nonebot`, `astrbot`  |

`BOT_EXPORTER` factories turn a configured instance into a chatbot-framework
plugin; see `docs/architecture/bot-export.md` and
`docs/plugin-development/bot-exporter.md`.

See `docs/plugin-development/` for authoring guides.
