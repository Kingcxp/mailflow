# Change playbook

Procedures for common change types. Always read `invariants.md` first and
run `make check` before committing.

## Add a processor

1. Create `plugins/mailflow-<name>/` following `mailflow-processor-rules`
   (pyproject with entry point, `plugin.py`, `__init__.py`).
2. Implement `MailProcessor.process`; use `context.options` and the
   injected `now` clock. Return a partial `MailAnalysis` overlay.
3. Register `registrar.add_processor("<component-id>", factory)` and declare
   `kinds=[ComponentKind.MAIL_PROCESSOR]` in `PluginInfo`.
4. Add a unit test with a canned completion (see
   `tests/unit/test_llm_processor.py`).
5. Reference it in config by component id; add an entry to
   `packages/mailflow-bundled` ONLY if it belongs to the official set.
6. Update `docs/plugin-development/processor.md` if behavior is generic.

## Add a mail source adapter

1. Implement `MailSource.run` (normalized UTC mails via `emit`) +
   `send_reply` + `close`.
2. Register `add_source("<component-id>", factory)`; the factory reads
   `MailAccountConfig.options`.
3. Test normalization and `send_reply` recording; for a fake, reuse
   `mailflow-testkit`.
4. Real providers are later-stage work: keep them out of the 0.1.0 baseline
   and mark them "planned" in `CHANGELOG.md`.

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
