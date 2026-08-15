# Tests

## Layout

```
tests/unit/        contract and logic tests (no I/O, no network)
  test_core.py         urgency contract, config defaults/validation, logging
  test_pipeline.py     event bus, LLM routing, pipeline semantics
  test_i18n.py         builtin/external packs, fallback, key parity
  test_runtime.py      retention scheduler, runtime isolation
  test_service.py      reply state machine (negative paths, double-send guard)
  test_commands.py     command router output and mutations
  test_llm_processor.py  JSON extraction, urgency normalization, exam action
tests/integration/  concrete adapters with fakes (never a real API)
  test_plugins.py      sqlite persistence/trash semantics; openai-compatible
                       transport via a monkeypatched httpx client; bundled
                       registration coverage
tests/e2e/          full flows through the public API
  test_start_service.py  start_service: source→queue→pipeline→storage, LLM
                       fallback, notifier, snapshot ownership, urgency reset,
                       reply confirm, commands, language persistence, trash
  test_tui.py          headless Textual pilot: compose, search, urgency,
                       language, reply gating
```

## Run

```bash
make test            # pytest -q
make coverage        # pytest with coverage (html + term)
```

Determinism: fake sources use fixed timestamps (`make_mail`), test LLMs
return canned JSON, httpx is monkeypatched. No test performs a real network
request.

## Writing tests

- Unit tests defend observable contracts: boundaries, transitions,
  precedence, real error paths — not plumbing or source text.
- Integration tests exercise plugin code but stub the transport boundary
  (httpx.AsyncClient, filesystem via tmp_path).
- E2E tests start only through `start_service(...)` with components
  registered through the normal Pluggy hooks, and stop through
  `service.stop()` in `finally`.
- New processors should follow `tests/unit/test_llm_processor.py`: canned
  completion → assert domain output (urgency, action items with mail_id
  backlink, notes) and backend identity.
