# Bot exporter plugins

A `bot_exporter` plugin turns a *configured* MailFlow instance into a
plugin for a chatbot framework (NoneBot, AstrBot, ...). `mailflow export
--framework <id>` and the TUI export wizard load exporters through the
component registry, so a new framework is a plugin — never a core change.
This page is the author's guide; the marketplace copy lives in the
[mailflow-repo docs](../../../mailflow-repo/docs/bot-exporter.md).

## Contract

Register one factory per framework id:

```python
from mailflow.domain import ComponentKind
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar
from mailflow.bot_export import BotExportContext, BotExportResult

PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-export-mybot",
    name="MyBot Exporter",
    version="0.1.0",
    description="Exports a configured MailFlow instance as a MyBot plugin",
    kinds=[ComponentKind.BOT_EXPORTER],
)


def export_mybot(context: BotExportContext) -> BotExportResult:
    # write the framework plugin package under context.output_dir
    return BotExportResult(
        framework="mybot",
        plugin_name="mybot_plugin_mailflow",
        created=["README.md", "main.py"],
        notes="notes shown to the user by the CLI/TUI",
    )


class MyPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config) -> None:
        registrar.add_bot_exporter("mybot", export_mybot)


plugin = MyPlugin()
```

`BotExportContext` fields:

- `config` — the resolved live `MailFlowConfig`; persist with
  `mailflow.config.write_config`.
- `plugin_ids` — enabled plugin ids; the generated plugin declares these
  (plus `mailflow-core` / `mailflow-bundled`) as dependencies.
- `output_dir` — directory to write into (created for you).
- `version` / `language` — MailFlow version and active language.

Return a `BotExportResult` listing every relative path written; the
framework id is what users pass to `--framework`.

## Rules for authors

- **Offline only.** The factory gets the config and registry, never a
  started service: no mail fetching, no LLM calls during export.
- **Synchronous and fast.** The CLI calls it directly, the TUI runs it in a
  worker; plain file I/O only.
- **Flag secrets.** The embedded config is resolved; remind users to swap
  real tokens for `${ENV_VAR}` placeholders (`BotExportResult.notes` +
  generated README).
- **Declare dependencies.** The generated plugin must install
  `mailflow-core`, `mailflow-bundled` and every `plugin_ids` package, then
  start with `start_service(config, plugin_manager=create_plugin_manager(config))`.
  It is a *host*, not a MailFlow component.
- **No chat commands.** Command surfaces belong to the framework; generate
  lifecycle-only plugins.

## Scaffolding and testing

- Wizard: Market tab → New → `bot_exporter` template category, or
  `mailflow.plugin_template.scaffold_plugin(..., category="bot_exporter")`.
- Unit-test the factory: build a `BotExportContext` with a `tmp_path`
  `output_dir`, run the factory, assert the created files and a round-trip
  `load_config` of the generated `config.toml`.
- The marketplace validator instantiates the factory with a scratch
  `BotExportContext`; reference implementations:
  `plugins/mailflow-export-nonebot` and `plugins/mailflow-export-astrbot`.
