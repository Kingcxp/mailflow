# Module map

## packages/mailflow-core/src/mailflow/

| Module | Owns |
| ------ | ---- |
| `domain.py` | Urgency + colors, MailMessage, MailAnalysis, ActionItem, MailRecord (effective_urgency), TrashRecord, ReplyDraft/ReplyState, snapshot models, CommandResponse/StyleSpan |
| `config.py` | typed TOML config, `${ENV}` interpolation, cross-reference validation |
| `contracts.py` | Protocols (MailSource, MailProcessor, LLMBackend, Notifier, StorageBackend, LLMRouter), ProcessorResult, ProcessingContext, LLMCompletion |
| `registry.py` | ComponentRegistry (typed factories + ownership snapshots), PluginRegistrar |
| `plugins.py` | PluginInfo, hookspecs, PluginManager (discovery, allow/deny, registry build) |
| `events.py` | async EventBus with wildcard subscriptions |
| `llm.py` | LLMRouterImpl (named routing, fallback, de-dup, secret redaction) |
| `pipeline.py` | ProcessorBinding, PipelineEngine (ordering, retries, timeout, policy), fallback summary, merge_analysis |
| `logging.py` | QueueHandler/Listener, rich console/file/jsonl sinks, SecretRedactionFilter, LoggingRuntime |
| `i18n.py` | builtin + external JSON packs, English fallback, language switch |
| `runtime.py` | bounded queue, per-account source tasks, workers, notifier thresholds, cleanup scheduler, wait_idle |
| `service.py` | MailFlowService facade, start_service composition, run_service, reply workflow |
| `commands.py` | CommandRouter: shlex parse, transport-neutral colored responses, all management commands (incl. config, plugin repo/market/install) |
| `plugin_market.py` | PluginMarket: fetch/validate marketplace indexes, find, install via uv pip --no-deps, installed-state checks |
| `locale/en.json`, `locale/zh-CN.json` | built-in language packs |

## packages/

| Package | Owns |
| ------- | ---- |
| `mailflow-bundled` | composition root: static registration of the official plugins, optional discovery |
| `mailflow-cli` | Typer host: run/command/shell/config-check/snapshot/doctor/tui; renders CommandResponse with rich |
| `mailflow-tui` | Textual app (Mail/Actions/Runtime/Logs/Settings), runner with injected log handler, app.tcss |
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

A sibling marketplace repository (parent directory `mailflow-repo`)
holds plugins one folder per plugin under category folders; the market client
reads a root `index.json` (categories only) and each plugin's `plugin.json`.

## Dependency direction

domain → config/contracts → registry → plugins → llm/pipeline → runtime →
service → commands. Hosts (cli/tui) → service/commands. Plugins → core
contracts only. Core never imports plugins or hosts.

## Tests

`tests/unit` (contracts), `tests/integration` (concrete backends with
monkeypatched transport), `tests/e2e` (public start_service + TUI pilot).
