# Module map

## packages/mailflow-core/src/mailflow/

| Module | Owns |
| ------ | ---- |
| `domain.py` | Urgency + colors, MailMessage, MailAnalysis, ActionItem, MailRecord (effective_urgency), TrashRecord, ReplyDraft/ReplyState, snapshot models, CommandResponse/StyleSpan |
| `config.py` | typed TOML config, `${ENV}` interpolation (recording placeholders so secrets are never written back), cross-reference validation |
| `settings.py` | editor-shaped view of the schema: sections per plugin, `OptionSpec`/`EditorKind`, `apply_value`/`reset_value`, list-entry add/update/remove/move, LLM fallback-chain derivation, `SettingsError` |
| `contracts.py` | Protocols (MailSource, HistoryCapableSource, MailProcessor, LLMBackend, Notifier, StorageBackend, LLMRouter), ProcessorResult, ProcessingContext, LLMCompletion |
| `registry.py` | ComponentRegistry (typed factories + ownership snapshots), PluginRegistrar |
| `plugins.py` | PluginInfo, hookspecs, PluginManager (discovery, allow/deny, registry build) |
| `events.py` | async EventBus with wildcard subscriptions |
| `llm.py` | LLMRouterImpl (named routing, fallback, de-dup, secret redaction) |
| `pipeline.py` | ProcessorBinding, PipelineEngine (ordering, retries, timeout, policy), fallback summary, merge_analysis |
| `logging.py` | QueueHandler/Listener, rich console/file/jsonl sinks, SecretRedactionFilter, LoggingRuntime |
| `i18n.py` | builtin + external JSON packs, English fallback, language switch |
| `runtime.py` | bounded queue, per-account source tasks, workers, notifier thresholds, cleanup scheduler, wait_idle |
| `service.py` | MailFlowService facade, start_service composition, run_service, reply workflow |
| `commands.py` | CommandRouter: shlex parse, transport-neutral colored responses, all management commands (incl. config, plugin repo/market/install); rich markdown→span rendering with span-color support |
| `plugin_market.py` | PluginMarket: fetch per-plugin metadata from category folders (file://, GitHub contents API, INDEX.json fallback), localized descriptions/readmes, find/search/install via uv pip --no-deps |
| `plugin_template.py` | Plugin scaffolding: category templates (mail_source/processor/llm_backend/notifier/storage/bot_exporter) that produce complete, loadable plugins; used by the TUI wizard |
| `bot_export.py` | Bot-framework export: BotExportContext/BotExportResult, available_frameworks, export_bot_plugin (single entry point used by CLI, TUI and make targets) |
| `locale/en.json`, `locale/zh-CN.json` | built-in language packs |

## packages/

| Package | Owns |
| ------- | ---- |
| `mailflow-bundled` | composition root: static registration of the official plugins, optional discovery |
| `mailflow-cli` | Typer host: run/command/shell/config-check/snapshot/doctor/export/tui; renders CommandResponse with rich |
| `mailflow-tui` | Textual app (Mail/Mailboxes/Actions/LLMs/Runtime/Logs/Market/Settings), settings editor (settings.py), export wizard (export.py), runner with injected log handler, app.tcss |
| `mailflow-testkit` | FakeMailSource, FakeLLMBackend, FakeNotifier, make_mail/fixed_timestamps |

## plugins/

| Plugin | Provides |
| ------ | -------- |
| `mailflow-mail-fake` | `fake` mail source (option-driven deterministic mails) |
| `mailflow-storage-sqlite` | `sqlite` storage (WAL, trash, drafts, preferences) |
| `mailflow-llm-openai-compatible` | `openai-compatible` LLM backend |
| `mailflow-processor-rules` | `rules` processor (ad keywords, important senders) |
| `mailflow-processor-llm-importance` | `llm-importance` processor (four-level analysis) |
| `mailflow-notify-console` | `console` notifier |
| `mailflow-export-nonebot` | `nonebot` bot exporter (generates a NoneBot2 plugin) |
| `mailflow-export-astrbot` | `astrbot` bot exporter (generates an AstrBot plugin) |

A sibling marketplace repository (`mailflow-repo`, pushed to
github.com/Kingcxp/mailflow-repo) holds plugins one folder per plugin under
category folders; the market client reads a root `index.json` (categories
only) and each plugin's `plugin.json`, which may carry localized
`descriptions` / `readmes`. The repo ships the developer docs (docs/), the
validation script (tools/validate_plugin.py) and a PR validation workflow.

## Dependency direction

domain → config/contracts → registry → plugins → llm/pipeline → runtime →
service → commands. Hosts (cli/tui) → service/commands. Plugins → core
contracts only. Core never imports plugins or hosts.

## Tests

`tests/unit` (contracts), `tests/integration` (concrete backends with
monkeypatched transport), `tests/e2e` (public start_service + TUI pilot).
