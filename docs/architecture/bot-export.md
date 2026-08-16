# Bot-framework export

MailFlow can be shipped as a plugin for a chatbot framework (NoneBot,
AstrBot, ...): `mailflow export --framework <id> --output <dir>` (CLI), the
TUI export wizard (Market tab → Export) and `make bot-plugin-*` all turn a
*configured* instance into a framework plugin package. The exporters are
plugins themselves — a plugin with kind `BOT_EXPORTER` registers one factory
per framework id, so supporting a new framework never touches the core.

## Design

```
Hosts (CLI export, TUI export wizard, make targets)
        │  mailflow.bot_export.export_bot_plugin(registry, config, ...)
        ▼
mailflow.bot_export        BotExportContext · BotExportResult
        │  registry.bot_exporter_factory(framework_id)
        ▼
plugins/* (kind BOT_EXPORTER)
        mailflow-export-nonebot    → generates nonebot-plugin-mailflow
        mailflow-export-astrbot    → generates astrbot_plugin_mailflow
        (your own)                 → any other framework
```

- `ComponentKind.BOT_EXPORTER` joins `MAIL_SOURCE` / `MAIL_PROCESSOR` /
  `LLM_BACKEND` / `NOTIFIER` / `STORAGE`. The framework id *is* the
  component id: `registrar.add_bot_exporter("nonebot", factory)` makes
  `registry.bot_exporter_factory("nonebot")` resolvable.
- `export_bot_plugin(registry, config, framework=..., output_dir=...)` is
  the single entry point shared by every host. It runs the registered
  factory synchronously with a `BotExportContext` (config, enabled plugin
  ids, output dir, version, language) and returns a `BotExportResult`
  (framework, plugin name, created files, notes).
- Export is **offline**: it needs the config and the registry only, never a
  started service. The generated plugin is a *host* — it declares
  `mailflow-core`, `mailflow-bundled` and every enabled plugin package as
  dependencies and boots the engine with `start_service` +
  `create_plugin_manager` inside the framework's lifecycle hooks.

## What the generated plugin contains

Both bundled exporters embed:

- the framework plugin package (`pyproject.toml` + driver hooks for
  NoneBot; `main.py` + `metadata.yaml` for AstrBot);
- `config.toml` — the **resolved** live configuration, serialized with
  `write_config`;
- dependency declarations covering every enabled MailFlow plugin.

Because the config is the resolved instance, secrets are embedded as-is;
both exporters tell the user (via `BotExportResult.notes` and the generated
README) to replace real tokens with `${ENV_VAR}` placeholders before
sharing the plugin.

## Host integration

- **CLI** — `mailflow export --framework nonebot --output dist/x -c config.toml`
  (Typer command in `mailflow-cli`; the body `run_export` is a pure function
  kept testable). Unknown frameworks print the available ids and exit 1.
- **TUI** — `BotExportScreen` (modal): framework `Select`, `DirectoryTree`
  folder pick, optional subfolder checkbox + input, Generate button running
  the export in a worker. Opened from the Market tab (`#market-export`).
- **make** — `make bot-plugin-nonebot` / `bot-plugin-astrbot` (fixed
  framework/output) and `make bot-plugin FRAMEWORK=<id> OUTPUT=<dir>`.

## Security

- The export never starts sources, LLMs or the pipeline — no mail traffic
  happens during generation.
- Secrets: `write_config` serializes the live config verbatim. Treat the
  output directory as secret-bearing until `${ENV_VAR}` placeholders are in
  place (the generated README and the CLI notes both say so).

## See also

- `plugin-system.md` — hooks, registries, ownership stamping
- `../plugin-development/bot-exporter.md` — writing your own exporter
- `../development/packaging.md` — wheels and frozen executables
- `tui.md` — the export wizard screen
