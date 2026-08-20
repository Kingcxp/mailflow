# Change playbook

Procedures for common change types. Always read `invariants.md` first and
run `make check` before committing.

## Add a processor

The two default processors (`rules`, `llm-importance`) are **built into the
core** — `mailflow/processors.py`, registered by `register_builtin_processors`
under the plugin id `mailflow-core`. Pick the smallest option that fits:

**A. Tune the built-in LLM analysis** — implement `LLMEnhancer`
(`contracts.py`) and register it with `registrar.add_llm_enhancer(...)`. You
get three optional hooks (`system_prompt`, `extra_messages`, `post_process`)
without reimplementing classification. This is the right seam for
"same four levels, extra domain knowledge".

**B. Add a new processor** — when you need your own decision:

1. Create `plugins/mailflow-<name>/` (pyproject with a `mailflow.plugins`
   entry point, `plugin.py`, `__init__.py`). Use the marketplace's
   `processor/mailflow-processor-blocklist` as the reference implementation,
   or scaffold one (`mailflow.plugin_template`, or the TUI Market → New
   wizard) which also emits the declarative `plugin_api` style.
2. Implement `MailProcessor.process`; use `context.options`, the injected
   `now` clock and `context.feedback_guidelines`. Return a **partial**
   `MailAnalysis` overlay — the pipeline merges overlays in priority order,
   so contribute only the fields you decided.
3. Register `registrar.add_processor("<component-id>", factory)`; the factory
   takes `(ProcessorConfig, LLMRouter)`. Declare
   `kinds=[ComponentKind.MAIL_PROCESSOR]` in `PluginInfo`.
4. Add a unit test with a canned completion (see
   `tests/unit/test_llm_processor.py`) or a pure input/output test for a
   deterministic processor.
5. Reference it in config by component id and give it a `priority` that puts
   it where it belongs in the chain (cheap deterministic filters before the
   LLM). Add it to `packages/mailflow-bundled` ONLY if it joins the official
   set.
6. Update `docs/plugin-development/processor.md` if the behavior is generic.

A plugin that registers an existing component id **wins**: the built-in is
skipped (`register_builtin_processors` checks `registry.has` first), so a
third-party `rules` replaces the default without a core change.

## Add a mail source adapter

1. Implement `MailSource.run` (normalized UTC mails via `emit`) +
   `send_reply` + `close`.
2. Optionally implement `fetch_history(limit, offset)` to satisfy
   `HistoryCapableSource`: the TUI Mailboxes tab can then browse mail that
   arrived before MailFlow was configured. Return newest-first and **never**
   emit; browsing must not disturb the live stream's incremental state (see
   `plugins/mailflow-mail-imap` for the UID handling).
3. Register `add_source("<component-id>", factory)`; the factory reads
   `MailAccountConfig.options`.
4. Test normalization, `send_reply` recording and — if implemented — that
   `fetch_history` pages newest-first without moving the poll water-mark. For
   a fake, reuse `mailflow-testkit`.

## Add a configurable option

1. Add the field to the right model in `config.py` with a `description` and
   validation constraints (`ge`/`le`, a model validator for cross-field
   rules). The editor kind is derived from the annotation, so no UI change is
   needed.
2. Add `config.desc.<dotted.key>` to **both** locale packs via
   `tools/gen_option_descriptions.py` (it fails when the en/zh key sets
   differ).
3. If the option is a secret, make sure its name matches `_SECRET_MARKERS` so
   it is redacted and rendered as a password field.
4. Extend `tests/unit/test_settings.py` when the option needs a new editor
   kind or coercion rule.

## Change the reply flow

Touch `service.py` reply methods + `domain.ReplyDraft`. Keep the three
safety properties (token gate, persist-before-send, failed-send revert).
Extend `tests/unit/test_service.py` negative paths.

## Add a language pack

1. Create `translations/<code>.json` (data only: `locale`, `name`,
   `messages`); partial packs fall back to English.
2. Add `translations` to `[i18n] extra_dirs` in configs.
3. Built-in languages require `locale/<code>.json` + parity test updates.

## Rename a symbol across files

Use the language server (`lsp` rename/references), never text replacement.
Then run `make check`; mypy/pyright catch stragglers.

## Modify the pipeline semantics

Update `pipeline.py` AND `docs/architecture/pipeline.md` in the same change;
extend `tests/unit/test_pipeline.py` (retry exhaustion, stop policy, timeout,
fallback summary).

## Release checklist (baseline tag)

```bash
uv sync --all-packages --group dev
make check
make build
make exe-standalone   # then smoke-test dist/frozen_entry.dist/frozen_entry.exe
make exe-onefile      # only after the standalone smoke test
```

Update `CHANGELOG.md`; record executed commands in
`docs/build-log/BUILD_LOG.md`; tag `vX.Y.Z` after the gates pass.
