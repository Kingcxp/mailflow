# MailFlow

Unified multi-account mail inbox with a plugin pipeline for filtering and
LLM-assisted analysis, a rich terminal UI, and an embeddable core service.

Mail accounts from different providers are merged into one stream, processed
by an ordered plugin chain (deterministic rules first, LLM semantics after),
stored with a seven-day recovery trash, and surfaced through a Textual TUI, a
command shell, or any chat-bot host embedding the core.

## Status

`v0.1.0` — framework baseline. The architecture, plugin system, storage,
OpenAI-compatible LLM backend, processing chain, CLI and TUI are complete and
covered by unit/integration/e2e tests. Real mail providers (IMAP, Gmail,
Outlook) are **deliberately scheduled as later provider plugins** and are not
part of this baseline.

## The four-level urgency contract

| Level      | Color    | Meaning                                                        |
| ---------- | -------- | -------------------------------------------------------------- |
| `ad`       | #909399  | irrelevant advertising / junk; ignore                          |
| `info`     | #67C23A  | useful but not time-critical (e.g. a lecture notice)           |
| `important`| #E6A23C  | needs reading (e.g. a verification code)                       |
| `urgent`   | #F56C6C  | must be handled now or at a specific time (exam, ID pickup)    |

The automatic value is produced by the processing chain; the user may set a
manual override which wins while set, and resetting restores the automatic
value. The colors are part of the public contract (`Urgency.color`) and are
reused by the CLI, TUI and notifiers.

## Layout

```
packages/mailflow-core       host-agnostic domain, pipeline, service facade
packages/mailflow-bundled    composition root: the official plugin set
packages/mailflow-cli        rich Typer host: run/command/shell/snapshot/...
packages/mailflow-tui        Textual terminal UI (Mail/Actions/Runtime/Logs/Settings)
packages/mailflow-testkit    deterministic fake components for tests and demos
plugins/*                    discoverable adapter/processor plugins
configs/                     example and development configurations
translations/                data-only external language packs
docs/                        architecture, development, plugin, agent documentation
```

## Quick start

```bash
uv sync --all-packages --group dev
uv run mailflow config-check -c configs/example.toml
cp configs/example.toml configs/local.toml   # then fill in tokens
uv run mailflow run -c configs/local.toml
```

Development setup without any external service:

```bash
uv run mailflow tui -c configs/development.toml
uv run mailflow shell -c configs/development.toml
```

## Command interface

One command router serves the CLI shell and any chat platform:

```
help                     colored command documentation
mail list|show|delete|urgency <id> <level|auto>
action list|show <item_id>
plugin list|show <plugin_id>
adapter list  account list  llm list|bindings  runtime
reply create|show|edit|prepare|confirm|cancel
lang get|set <code>      trash list|restore <id>
```

Replies require a two-step confirmation: `prepare` issues a short-lived token,
`confirm` validates it and sends through the matching mail source.

## Embedding (chat bots, other hosts)

```python
from mailflow.service import start_service
from mailflow_bundled import create_plugin_manager

config = load_config("configs/local.toml")
service = await start_service(
    config,
    plugin_manager=create_plugin_manager(config),
    extra_log_handlers=[my_host_handler],
)
# service.snapshot(), service.list_mails(), service.commands.execute("mail list"), ...
# await service.stop()
```

The service exposes everything a host needs: runtime snapshots (plugins,
adapters, accounts, LLMs, processor→LLM bindings), mail/action/trash queries,
urgency mutations, persistent language, and the confirmed reply workflow.
See `docs/development/embedding.md`.

## Configuration

See `configs/example.toml` and `docs/configuration/overview.md`. Secrets use
whole-string `${ENV_VAR}` placeholders expanded at load time. Mail retention
(default 30 days) and trash retention (7 days) are configurable; a cleanup
runs daily at 04:00 local time.

## i18n

English (default) and Simplified Chinese ship built-in; other languages load
as data-only JSON packs from configured directories. Switch with `lang set`
or the TUI settings tab; the choice persists across restarts. See
`docs/configuration/i18n.md`.

## Quality gates

```bash
make check          # lint + format-check + mypy + pyright + pytest + docs
make coverage
make build          # wheel build for every package
make exe-standalone # Nuitka standalone; smoke test before onefile
make exe-onefile
```

## Documentation

- `docs/architecture/` — how MailFlow works
- `docs/development/` — setup, embedding, testing, quality, packaging
- `docs/plugin-development/` — write your own adapters and processors
- `docs/configuration/` — runtime configuration and i18n
- `docs/agent/` — invariants, module map and change playbook for AI agents
- `docs/adr/` — architecture decision records
- `MAILFLOW_FROM_ZERO.md` — the staged reconstruction plan
- `docs/build-log/BUILD_LOG.md` — what was actually executed and verified
