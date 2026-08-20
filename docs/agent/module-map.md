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
| `processors.py` | the built-in `rules` and `llm-importance` processors, SYSTEM_PROMPT, JSON extraction, `register_builtin_processors` (registered as plugin id `mailflow-core`) |
| `logging.py` | QueueHandler/Listener, rich console/file/jsonl sinks, SecretRedactionFilter, LoggingRuntime |
| `i18n.py` | builtin + external JSON packs, English fallback, language switch |
| `runtime.py` | bounded queue, per-account source tasks, workers, notifier thresholds, cleanup scheduler, wait_idle |
| `service.py` | MailFlowService facade, start_service composition, run_service, reply workflow |
| `commands.py` | CommandRouter: shlex parse, transport-neutral colored responses, all management commands (incl. config, plugin repo/market/install); rich markdown→span rendering with span-color support |
| `plugin_market.py` | PluginMarket: fetch per-plugin metadata from category folders (file://, GitHub contents API, INDEX.json fallback), localized descriptions/readmes, find/search/install via uv pip --no-deps |
| `plugin_template.py` | Plugin scaffolding: category templates (mail_source/processor/llm_backend/notifier/storage/bot_exporter) that produce complete, loadable plugins; used by the TUI wizard |
| `bot_export.py` | Bot-framework export: BotExportContext/BotExportResult, available_frameworks, export_bot_plugin (single entry point used by CLI, TUI and make targets) |
| `plugin_api.py` | declarative authoring: `define_plugin` + per-kind decorators that build both pluggy hooks |
| `updates.py` | release/plugin version checks, update-source gating, `apply_plugin_updates`, `upgrade_mailflow` |
| `letters.py` | formal-letter templates (cn/en), markup→html, html→text |
| `locale/en.json`, `locale/zh-CN.json` | built-in language packs (458 keys each, parity enforced) |

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
| `mailflow-mail-imap` | `imap` mail source (IMAP polling + SMTP replies, provider presets, history capability) |
| `mailflow-mail-fake` | `fake` mail source (option-driven deterministic mails, dev/demo only) |
| `mailflow-storage-sqlite` | `sqlite` storage (WAL, trash, drafts, preferences, custom actions) |
| `mailflow-llm-openai-compatible` | `openai-compatible` LLM backend |
| `mailflow-llm-anthropic` | `anthropic` LLM backend (Claude Messages API) |
| `mailflow-notify-console` | `console` notifier |
| `mailflow-export-nonebot` | `nonebot` bot exporter (generates a NoneBot2 plugin) |
| `mailflow-export-astrbot` | `astrbot` bot exporter (generates an AstrBot plugin) |

The `rules` and `llm-importance` processors are **not** plugins: they live in
`mailflow/processors.py` and are registered by `register_builtin_processors`
under the plugin id `mailflow-core`. A plugin registering the same component
id replaces the built-in.

A sibling marketplace repository (`mailflow-repo`, pushed to
github.com/Kingcxp/mailflow-repo) holds plugins one folder per plugin under
category folders; the market client reads a root `index.json` (categories
only) and each plugin's `plugin.json`, which may carry localized
`descriptions` / `readmes`. The repo ships the developer docs (docs/), the
validation script (tools/validate_plugin.py) and a PR validation workflow.

## tools/

| Script | Owns |
| ------ | ---- |
| `check_docs.py` | the docs gate: mandatory documents present, and every doc cross-checked against the code (paths, make targets, event names, service methods, plugin ids) |
| `gen_option_descriptions.py` | regenerates the `config.desc.*` block of both locale packs; refuses to write when the en/zh key sets differ |
| `build_all.py`, `build_exe.py` | wheels for every package; Nuitka standalone/onefile |
| `clean.py` | remove caches, build output and local runtime data |
| `annotate_pytest_failures.py` | turn pytest failures into CI annotations |

## Dependency direction

domain → config/settings/contracts → registry → plugins → llm/pipeline →
runtime → service → commands. Hosts (cli/tui) → service/commands. Plugins →
core contracts only. Core never imports plugins or hosts.

## Tests

`tests/unit` (contracts, settings editor, commands, i18n, pipeline, runtime,
letters, updates, plugin api/template, bot export), `tests/integration`
(concrete backends with monkeypatched transport), `tests/e2e` (public
`start_service` + TUI pilot). 317 tests at the time of writing; the per-file
breakdown lives in `../development/tests.md`.
