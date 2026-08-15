# MailFlow from zero: file-by-file construction and Git commit plan

This document is written for a small coding agent that will rebuild MailFlow locally while producing reviewable Git commits. It is **not** a fabricated history of this generated repository. Treat every stage as a target state: edit the named files, run the stated verification, inspect the diff, and only then create the suggested commit.

## Rules for the agent

- Work in stage order. Later stages assume earlier public contracts exist.
- Do not create placeholder commits with empty or non-working files.
- Do not claim a check passed unless the command was actually executed.
- If dependency download is blocked, record that limitation and run local `compileall`/available tests; rerun the full gate when networking returns.
- Never commit real mailbox credentials or LLM tokens.
- Keep real Gmail/Outlook/IMAP implementation out of Stages 00–20. Those are explicitly future provider stages.
- After every stage, run `git diff --check` before committing.
- Use the commit subject shown here unless local work materially changes the scope.

---

## Stage 00 — Bootstrap the uv workspace

**Goal:** create a repository that has no business logic yet, but has a stable workspace, task entry points, ignore rules and quality-tool configuration.

### Edit order

1. **CREATE `.gitignore`.** Ignore `.venv/`, Python caches, coverage, build/dist output, local databases, logs and secret local config files.
2. **CREATE root `pyproject.toml`.** Add Python >=3.11, `[tool.uv] package=false`, workspace members `packages/*` and `plugins/*`, the dev dependency group, pytest/Ruff/mypy/Pyright settings.
3. **CREATE `Makefile`.** Add `sync`, `test`, `coverage`, `lint`, `format`, `format-check`, `mypy`, `pyright`, `typecheck`, `check`, `run`, `tui`, `build`, `exe-*`, `docs`, `clean` targets.
4. **CREATE `tools/clean.py`.** Keep cleanup logic cross-platform instead of putting complex shell commands into Make.
5. **CREATE `README.md` only with a temporary project header and setup command.** Do not write architecture claims before code exists.

### Verify

```bash
uv --version
python --version
uv lock
python -m compileall tools
```

If `uv lock` cannot reach package indexes, do not commit a fake `uv.lock`; retry in a connected environment.

### Commit

```text
chore: bootstrap uv workspace and quality toolchain
```

Suggested body:

```text
- define the multi-package uv workspace
- centralize pytest, Ruff, mypy and Pyright settings
- add Makefile task entry points and cross-platform cleanup helper
```

---

## Stage 01 — Create the Core package and domain contracts

**Goal:** define provider-independent data types before adding any network adapter or UI.

### Edit order

1. **CREATE `packages/mailflow-core/pyproject.toml`.** Depend only on Core-level libraries (`pydantic`, `pluggy`, `rich`).
2. **CREATE `packages/mailflow-core/src/mailflow/domain.py`.** Add:
   - `Urgency` with the exact four values and colors;
   - addresses, attachments and normalized `MailMessage`;
   - `ActionItem` with `mail_id` backlink;
   - `MailAnalysis`;
   - `ProcessorNote`;
   - `MailRecord` with `manual_urgency` and computed `effective_urgency`;
   - safe reply draft models;
   - plugin/component/runtime snapshot models;
   - structured command response models.
3. **CREATE `packages/mailflow-core/src/mailflow/__init__.py`.** Export only intended public names.
4. **CREATE `tests/unit/test_core.py`.** First test fixes the urgency enum/value/color contract.

### Design checks

- `MailMessage` contains original normalized body/HTML; analysis is separate.
- Manual urgency never overwrites automatic urgency.
- Runtime snapshot types do not import concrete adapters.

### Verify

```bash
python -m compileall packages/mailflow-core/src
uv run pytest tests/unit/test_core.py -q
```

### Commit

```text
feat(core): define normalized mail and urgency domain contracts
```

---

## Stage 02 — Add validated configuration

**Goal:** make all runtime behavior configuration-driven and keep secrets outside source control.

### Edit order

1. **CREATE `mailflow/config.py`.** Add Pydantic models for general, logging, plugins, accounts, LLMs, processors, notifications, storage and i18n.
2. Add `${ENV_VARIABLE}` interpolation that only expands whole-string placeholders.
3. Validate named LLM default/fallback references.
4. Add defaults: English, configurable timezone, 30-day mail retention, seven-day trash retention, cleanup at 04:00, bounded queue and worker count.
5. Extend unit tests for the retention/cleanup defaults and invalid LLM references.

### Verify

```bash
uv run pytest tests/unit/test_core.py -q
```

### Commit

```text
feat(core): add typed runtime configuration and environment interpolation
```

---

## Stage 03 — Define component protocols and registries

**Goal:** establish inversion-of-control boundaries before writing plugins.

### Edit order

1. **CREATE `mailflow/contracts.py`.** Define protocols for `MailSource`, `MailProcessor`, `LLMBackend`, `Notifier`, `StorageBackend` plus processor context/result.
2. **CREATE `mailflow/registry.py`.** Define typed component factories and `ComponentRegistry`.
3. Implement `PluginRegistrar`; every registration must record exact `plugin_id`, kind and component ID.
4. Avoid "find the first plugin with this capability" logic. Ownership is assigned at registration time.

### Verify

```bash
python -m compileall packages/mailflow-core/src/mailflow
```

### Commit

```text
feat(core): introduce typed component contracts and ownership registry
```

---

## Stage 04 — Add Pluggy discovery without using it as the processing pipeline

**Goal:** support independently installed plugins while preserving explicit processor ordering.

### Edit order

1. **CREATE `mailflow/plugins.py`.** Define Pluggy hook spec and implementation marker.
2. Add hooks `mailflow_plugin_info()` and `mailflow_register(registrar, config)`.
3. Discover Python entry points in group `mailflow.plugins`.
4. Respect optional plugin allow/deny configuration.
5. Keep `ComponentRegistry` reset/ownership behavior deterministic for one service startup.

### Verify

```bash
python -m compileall packages/mailflow-core/src/mailflow
```

### Commit

```text
feat(core): add entry-point plugin discovery with pluggy
```

---

## Stage 05 — Add events, LLM routing and processing pipeline

**Goal:** create the internal dataflow with explicit failure semantics.

### Edit order

1. **CREATE `mailflow/events.py`.** Add a lightweight async event bus for runtime/program clients.
2. **CREATE `mailflow/llm.py`.** Add named LLM routing with ordered fallback and de-duplication.
3. **CREATE `mailflow/pipeline.py`.** Add sorted `ProcessorBinding` execution with per-processor timeout, retries and `failure_policy`.
4. Append `ProcessorNote` for success and failures.
5. Implement the final fallback-summary guarantee when no processor creates a summary.
6. Add fake LLM tests for primary failure -> backup success.
7. Add pipeline tests proving `continue` really continues after retries are exhausted.

### Verify

```bash
uv run pytest tests/unit -q
```

### Commit

```text
feat(core): add event bus, llm fallback routing and ordered processor pipeline
```

---

## Stage 06 — Build logging and secret redaction

**Goal:** support rich terminal output and durable/re-routable logs without taking over a host application's logging configuration.

### Edit order

1. **CREATE `mailflow/logging.py`.** Add:
   - MailFlow logger hierarchy;
   - `QueueHandler` / `QueueListener`;
   - Rich console handler;
   - rotating text file;
   - optional JSONL file;
   - injectable host handlers;
   - per-logger levels;
   - redaction filter.
2. Verify Core never invokes `basicConfig()` and `propagate=False` is scoped to `mailflow` logger.
3. Add a test that captures the root handler list before/after configuration.
4. Add a test that bearer/API token text is redacted.

### Verify

```bash
uv run pytest tests/unit/test_core.py -q
```

### Commit

```text
feat(core): add queue-based rich logging with secret redaction
```

---

## Stage 07 — Implement JSON i18n and persistent-language contract

**Goal:** make UI text data-driven and keep external language packs executable-code-free.

### Edit order

1. **CREATE `mailflow/i18n.py`.** Load built-in resource JSON, then external configured directories; fall back to English per missing key.
2. **CREATE `mailflow/locale/en.json`.** English is complete baseline/default.
3. **CREATE `mailflow/locale/zh-CN.json`.** Provide Simplified Chinese equivalents.
4. Add language listing/switching tests.
5. Later storage stage will persist the chosen locale.

### Verify

```bash
uv run pytest tests/unit/test_core.py -q
```

### Commit

```text
feat(core): add english chinese and external json localization
```

---

## Stage 08 — Implement the asynchronous runtime supervisor

**Goal:** merge multiple source adapters into a bounded common stream and isolate failures.

### Edit order

1. **CREATE `mailflow/runtime.py`.** Add:
   - bounded `asyncio.Queue`;
   - one source task per configured account;
   - configurable pipeline workers;
   - notification threshold application;
   - account error capture;
   - event emission;
   - graceful task cancellation.
2. Add `seconds_until_next_cleanup()` using `ZoneInfo` and local configured 04:00.
3. Add `run_cleanup()` that moves old active mail to trash and purges old trash.
4. Never let one source exception cancel all other sources.

### Verify

```bash
python -m compileall packages/mailflow-core/src/mailflow/runtime.py
```

### Commit

```text
feat(core): add bounded async runtime and daily retention scheduler
```

---

## Stage 09 — Build the public service facade and safe reply state machine

**Goal:** expose everything a CLI, TUI or bot needs through one stable object.

### Edit order

1. **CREATE `mailflow/service.py`.** Compose storage, plugins, LLMs, processors, sources, notifiers, events and runtime.
2. Add public query APIs:
   - runtime snapshot;
   - mail list/detail;
   - action list;
   - trash list.
3. Add public mutations:
   - manual urgency set/reset;
   - trash/restore;
   - persistent language.
4. Add reply workflow:
   - create;
   - get/edit;
   - prepare creates a short-lived token;
   - confirm validates token/expiry and calls only the matching source instance;
   - cancel.
5. **Implement `async start_service(...) -> MailFlowService`.** Allow a preconfigured `PluginManager`, plugin-discovery toggle, output stream and additional log handlers.
6. Add `run_service()` only as a standalone convenience wrapper around `asyncio.run()`.

### Verify

```bash
python -m compileall packages/mailflow-core/src/mailflow/service.py
```

### Commit

```text
feat(core): expose embeddable service facade and confirmed reply workflow
```

---

## Stage 10 — Add the shared command router

**Goal:** make QQ/chat platforms and CLI use the same management operations rather than duplicating command behavior.

### Edit order

1. **CREATE `mailflow/commands.py`.** Parse with `shlex` and return structured colored `CommandResponse`.
2. Add help and commands for:
   - mail list/show/delete/restore/urgency;
   - action list/show;
   - plugin list/show;
   - account listing;
   - LLM listing and processor bindings;
   - reply create/show/edit/prepare/confirm/cancel;
   - language get/set;
   - trash list/restore.
3. Keep command output transport-neutral: Rich styling is metadata, not ANSI bytes embedded in Core strings.

### Verify

```bash
python -m compileall packages/mailflow-core/src/mailflow/commands.py
```

### Commit

```text
feat(core): add transport-neutral management command router
```

---

## Stage 11 — Add the TestKit and fake components

**Goal:** test the complete framework without external accounts or paid APIs.

### Edit order

1. **CREATE `packages/mailflow-testkit/pyproject.toml`.** Depend on Core only.
2. **CREATE `mailflow_testkit/fakes.py`.** Add deterministic fake mail source, LLM backend, notifier and `make_mail()` helper.
3. **CREATE `plugins/mailflow-mail-fake/`.** Turn the fake source into a discoverable plugin for local/demo configurations.
4. Ensure `send_reply()` records calls for E2E assertions.

### Verify

```bash
python -m compileall packages/mailflow-testkit plugins/mailflow-mail-fake
```

### Commit

```text
test: add reusable fake mail llm and notifier components
```

---

## Stage 12 — Implement SQLite storage including recoverable trash

**Goal:** make all Core state durable with explicit trash semantics.

### Edit order

1. **CREATE `plugins/mailflow-storage-sqlite/pyproject.toml`** with `mailflow.plugins` entry point.
2. **CREATE plugin implementation.** Use a connection guarded by an async lock and WAL mode.
3. Create logical stores/tables for active mails, `trash_records`, drafts and preferences.
4. Implement manual urgency by reserializing the domain record.
5. Manual/automatic deletion moves the full serialized record to trash and stamps deletion time.
6. Restore must recover the same mail record.
7. Purge compares the trash timestamp, not original mail receipt time.
8. Add integration tests for save/manual urgency/trash/restore.

### Verify

```bash
uv run pytest tests/integration/test_plugins.py -q
```

### Commit

```text
feat(storage): add sqlite persistence with seven-day recovery trash
```

---

## Stage 13 — Implement the OpenAI-compatible Chat Completions backend

**Goal:** keep transport generic enough for OpenCode Go relays, local llama.cpp and other compatible services.

### Edit order

1. **CREATE `plugins/mailflow-llm-openai-compatible/pyproject.toml`.** Depend on Core and `httpx`.
2. Implement async POST to `base_url + path`.
3. Add Bearer auth only when a token is configured.
4. Merge configured headers/query/request/extra-body with per-call options without exposing secrets in logs.
5. Parse `choices[0].message.content` into `LLMCompletion` and record backend/model identity.
6. Add bounded transport retries.
7. Add an integration test with a monkeypatched `httpx.AsyncClient`, never a real API request.

### Verify

```bash
uv run pytest tests/integration/test_plugins.py -q
```

### Commit

```text
feat(llm): add configurable openai-compatible chat completions backend
```

---

## Stage 14 — Add deterministic rules and structured semantic analysis processors

**Goal:** provide the first real processing chain and map LLM JSON into the MailFlow domain.

### Edit order

1. **CREATE `plugins/mailflow-processor-rules/`.** Add cheap deterministic signals for obvious ads/senders before LLM work.
2. **CREATE `plugins/mailflow-processor-llm-importance/`.** Prompt with the exact four-level semantics.
3. Parse JSON fields:
   - summary;
   - urgency;
   - reason;
   - reply_required;
   - suggested_reply;
   - action_items;
   - notes.
4. Map action items to `ActionItem` including source `mail_id`.
5. Accept fenced JSON for imperfect compatible endpoints.
6. Route the request through `LLMRouter`; record the backend actually used.
7. Add a unit test for a critical exam requiring student ID.

### Verify

```bash
uv run pytest tests/unit/test_llm_processor.py -q
```

### Commit

```text
feat(processors): classify mail urgency replies and timed actions
```

---

## Stage 15 — Add baseline notifier and public-entry E2E

**Goal:** prove the architecture from the public startup API rather than constructing internals directly.

### Edit order

1. **CREATE `plugins/mailflow-notify-console/`.** Notify using the existing computed mail analysis.
2. **CREATE `tests/e2e/test_start_service.py`.** Register deterministic test components through the normal Pluggy hooks.
3. Start only with `await start_service(...)`.
4. Assert:
   - fake source -> queue -> pipeline -> storage;
   - primary LLM fails and backup handles classification;
   - critical exam action is stored;
   - notifier is invoked;
   - snapshot maps account/processor/LLM to the providing plugin;
   - manual urgency changes effective urgency and reset restores automatic result;
   - reply draft must prepare+confirm and calls fake source;
   - command router can show mail;
   - language can switch;
   - trash can restore.
5. Stop through `await service.stop()`.

### Verify

```bash
uv run pytest tests/e2e/test_start_service.py -q
uv run pytest -q
```

### Commit

```text
test: cover public service startup with full fake end-to-end flow
```

---

## Stage 16 — Add the Typer/Rich CLI host

**Goal:** expose standalone service operation and a reusable command-shell bridge without business logic in CLI callbacks.

### Edit order

1. **CREATE `packages/mailflow-bundled/`.** Add a small composition package that depends on the official built-in plugins, explicitly registers them, and optionally discovers additional entry-point plugins. This keeps concrete plugins out of Core and makes frozen builds independent of entry-point metadata for the official set.
2. **ADD `tests/integration/test_plugins.py` bundled registration coverage.** Assert the official source/processor/LLM/notifier/storage components are all present.
3. **CREATE `packages/mailflow-cli/pyproject.toml`.** Add Core, Bundled, Rich and Typer; create `mailflow` script.
4. **CREATE `mailflow_cli/app.py`.** Add:
   - `run` foreground service;
   - `command` one Core command;
   - `shell` persistent interactive Core command session;
   - `config-check` without network calls;
   - `snapshot` human/JSON view;
   - `doctor` runtime registration summary;
   - `tui` optional Textual host launcher.
5. Create the service with the bundled manager and keep extra entry-point discovery enabled through that composition package.
6. Ensure each started service is stopped in `finally`.
7. Do not duplicate mail/reply/urgency behavior already present in `CommandRouter`.

### Verify

```bash
uv run mailflow --help
uv run mailflow config-check -c configs/development.toml
```

### Commit

```text
feat(cli): add rich standalone host and shared command shell
```

---

## Stage 17 — Add the Textual TUI host

**Goal:** provide an administrative terminal UI that is a client of Core, not another implementation of MailFlow.

### Edit order

1. **CREATE `packages/mailflow-tui/pyproject.toml`.** Depend on Core, Bundled, Rich, Textual.
2. **CREATE `mailflow_tui/runner.py`.** Build the bundled plugin manager, start one service, attach a TUI log handler, run app, stop service.
3. **CREATE `mailflow_tui/app.py`.** Add Mail, Actions, Runtime, Logs and Settings tabs.
4. Mail tab:
   - search Input with placeholder;
   - urgency-colored DataTable;
   - selected mail summary, processor reasons and original body;
   - explicit urgency select/help;
   - trash/reply controls.
5. Actions tab:
   - time/type/content/notes/source-mail columns;
   - detail view and source-mail drill-down.
6. Runtime tab:
   - plugin list;
   - mail adapters;
   - accounts/status/errors;
   - LLMs/provider plugin;
   - processor -> LLM/fallback mapping.
7. Logs tab uses RichLog fed by injected handler; do not scrape stdout.
8. Settings contains language selector and short explanation.
9. Reply modal must have TextArea placeholder and separate Save / Prepare / Confirm controls.
10. **CREATE `app.tcss`.** Keep layout readable in normal terminal dimensions.

### Verify

```bash
uv run mailflow tui -c configs/development.toml
```

Manual checklist:

- keyboard and pointer can select a mail row;
- summary and original body are both visible;
- each editor/input has placeholder when empty;
- unclear controls have one-line help;
- urgency colors match the public contract;
- a reply cannot send before confirm;
- runtime mappings are readable without opening config files.

### Commit

```text
feat(tui): add mail action runtime log and settings interface
```

---

## Stage 18 — Add example configuration and external language-pack sample

**Goal:** make the baseline runnable without guessing TOML structure.

### Edit order

1. **CREATE `configs/example.toml`.** Show all current configuration groups and environment-variable tokens.
2. **CREATE `configs/development.toml`.** Keep it safe for local framework testing; use fake source.
3. **CREATE `translations/example.ja.json`.** Demonstrate a data-only external locale.
4. Document that a partial locale falls back to English.

### Verify

```bash
uv run mailflow config-check -c configs/example.toml
```

### Commit

```text
docs(config): add complete runtime and language-pack examples
```

---

## Stage 19 — Add build/package tooling

**Goal:** support wheel builds and reproducible frozen distributions without conflating uv and Nuitka responsibilities.

### Edit order

1. **CREATE `tools/build_all.py`.** Delegate normal package builds to `uv build --all-packages`.
2. **CREATE `tools/frozen_entry.py`.** Stable executable entry point calling CLI.
3. **CREATE `tools/build_exe.py`.** Invoke Nuitka in standalone or onefile mode and explicitly include `mailflow-bundled` plus the built-in plugin packages.
4. Fix Makefile `exe-standalone` and `exe-onefile` targets to match script arguments.
5. Add a release note that standalone must be tested before onefile and that arbitrary post-build Python plugin discovery is not promised for frozen mode.

### Verify

```bash
make build
make exe-standalone
# only after standalone smoke test:
make exe-onefile
```

### Commit

```text
build: add workspace wheel and nuitka executable workflows
```

---

## Stage 20 — Complete human/agent documentation and enforce doc presence

**Goal:** make the repository understandable to a human reviewer and constrained enough for a coding agent to modify safely.

### Edit order

1. **EXPAND root `README.md`** with status, urgency contract, layout, commands, embedding, quality gates.
2. **CREATE `CHANGELOG.md`.** Mark real providers as planned, not implemented.
3. **CREATE root `AGENTS.md`.** Agent first-entry rules.
4. **CREATE `docs/architecture/`** pages for overview, domain/mail, plugins, pipeline, LLM, logging, storage/retention, replies and TUI.
5. **CREATE `docs/development/`** setup, embedding, tests, quality, packaging.
6. **CREATE `docs/plugin-development/`** component author guides.
7. **CREATE `docs/configuration/`** overview and i18n schema.
8. **CREATE `docs/agent/`** invariants, module map and change playbook. Do not use the broad `docs/ai` name.
9. **CREATE ADR files** for uv workspace, Pluggy+pipeline separation and host-independent Core.
10. **CREATE this `MAILFLOW_FROM_ZERO.md`.** Keep future provider stages clearly separated.
11. **CREATE `tools/check_docs.py`.** Fail if mandatory architecture/agent/build-log documents are missing.

### Verify

```bash
make docs
make check
```

### Commit

```text
docs: document mailflow architecture agent rules and reconstruction plan
```

---

# Future provider phase — do not claim completed in the 0.1.0 baseline

The following stages are the recommended continuation after Stage 20 is green. They may be implemented by the local agent next, but must produce their own real tests/commits.

## Stage 21 — Generic IMAP source

**Goal:** first production mailbox provider with the smallest vendor-specific surface.

### Planned file order

1. Create `plugins/mailflow-mail-imap/pyproject.toml`.
2. Create provider config parsing under the plugin's own options schema/helpers.
3. Implement secure IMAP connection, folder selection, incremental UID tracking and MIME normalization.
4. Add reply/send only if SMTP or a clearly configured outbound transport is supplied; do not pretend IMAP itself sends mail.
5. Add provider cursor persistence strategy.
6. Add fixture-based MIME tests and a fake IMAP integration harness.
7. Add docs for TLS, app passwords/OAuth and server-specific caveats.

### Commit

```text
feat(mail): add incremental imap source adapter
```

---

## Stage 22 — Gmail API source

**Goal:** Gmail OAuth/API provider with incremental history synchronization and correct threading/reply behavior.

### Planned file order

1. Create `plugins/mailflow-mail-gmail/` package and entry point.
2. Add OAuth credential/token storage abstraction; never put tokens into TOML logs.
3. Implement initial synchronization and persist Gmail cursor/history identifier.
4. Implement incremental fetch and normalization.
5. Implement reply using provider thread/message metadata.
6. Test message parsing, cursor advancement, expired-history recovery and send/reply with mocked Google client responses.
7. Add human setup docs.

### Commit

```text
feat(mail): add gmail api source and reply adapter
```

---

## Stage 23 — Outlook / Microsoft Graph source

**Goal:** Microsoft account provider using Graph with incremental/delta synchronization.

### Planned file order

1. Create `plugins/mailflow-mail-outlook/`.
2. Add OAuth/token abstraction and Graph transport.
3. Implement initial and delta mail sync with cursor persistence.
4. Normalize recipients/body/attachments/threading.
5. Implement reply through Graph.
6. Test delta pagination, cursor expiration/recovery, MIME/body variants and send errors with mocked HTTP responses.
7. Add Azure app-registration setup docs.

### Commit

```text
feat(mail): add microsoft graph outlook source adapter
```

---

## Stage 24 — Production notification adapters and provider contract suite

**Goal:** turn the framework baseline into a practical notification service and make all providers satisfy common behavior.

### Planned work

1. Add notifier plugins selected for the deployment (for example ntfy, Gotify, Telegram/QQ bridge callback) without putting network clients into Core.
2. Expand `mailflow-testkit` with reusable source/notifier/storage contract tests.
3. Require each real mail source to pass normalization and reply-safety contracts.
4. Add retry/rate-limit tests.
5. Add CI matrix for supported Python/OS versions.
6. Add frozen-build smoke tests for every plugin included in release executables.

### Commit

```text
feat: add production notifications and plugin contract suite
```

---

# Final verification checklist for the local agent

Before declaring the baseline rebuilt:

```bash
uv sync --all-packages --group dev
uv run ruff check .
uv run ruff format --check .
uv run mypy packages plugins tools
uv run pyright
uv run pytest -q
uv run python tools/check_docs.py
uv build --all-packages
```

Then manually test:

```bash
uv run mailflow --help
uv run mailflow config-check -c configs/development.toml
uv run mailflow doctor -c configs/development.toml
uv run mailflow command "help" -c configs/development.toml
uv run mailflow tui -c configs/development.toml
```

Review the TUI at a normal terminal size, not only a huge development window. Verify placeholders, descriptions, urgency colors, mail original-content drill-down, action source backlinks, reply confirmation and language persistence.

Only after these checks should the agent create a baseline tag such as `v0.1.0`.
