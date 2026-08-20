# Tests

## Layout

317 tests (counts per file as of this writing):

```
tests/unit/                       contract and logic tests (no I/O, no network)
  test_core.py            42  urgency contract, config defaults/validation,
                              logging isolation/redaction, secret write-back,
                              section-scoped TOML patching
  test_settings.py        30  settings editor: editor kinds per field type,
                              coercion, validation errors naming the option,
                              reset-to-default, list-entry CRUD, LLM chain
                              order/derivation and reference scrubbing
  test_commands.py        38  command router output and mutations
  test_runtime.py         22  retention/reminder schedulers, digest, per-account
                              isolation, dedup
  test_pipeline.py        20  event bus, LLM routing, pipeline semantics
  test_plugin_template.py 19  every scaffold category generates a loadable plugin
  test_bot_export.py      15  export context/result, generated file sets
  test_service.py         15  reply state machine (negative paths, double-send
                              guard), mailbox history + on-demand processing
  test_llm_processor.py   14  JSON extraction, urgency normalization, actions
  test_letters.py         13  letter templates, markup→html, html→text
  test_updates.py         13  release/plugin version checks, auto-update gating
  test_i18n.py            11  builtin/external packs, fallback, key parity
  test_market.py           9  marketplace index parsing, search, install specs
  test_tui_runner.py       4  console-sink isolation, config_path wiring
  test_cli_export.py       4  `mailflow export` argument handling
  test_plugin_api.py       3  declarative decorators build both hooks
tests/integration/                concrete adapters with fakes (never a real API)
  test_plugins.py         28  sqlite persistence/trash semantics; the
                              openai-compatible and anthropic transports via a
                              monkeypatched httpx client; IMAP MIME parsing,
                              SMTP replies, UID-incremental polling, retry
                              safety and history paging; bundled registration
tests/e2e/                        full flows through the public API
  test_start_service.py    2  start_service: source→queue→pipeline→storage, LLM
                              fallback, notifier, snapshot ownership, urgency
                              reset, reply confirm, commands, language, trash
  test_tui.py             11  headless Textual pilot: compose/search/urgency,
                              language switch, reply gating, settings cards
                              (save / invalid / restore default), LLM chain
                              reorder, mailbox history analyze-selected,
                              repository dialog Back button, market detail,
                              scaffold + export wizards, processed-mail event
```

`test_start_service.py` and `test_tui.py` hold few but long scenarios: each
one drives a whole service lifecycle, so assertions are dense inside a single
test rather than spread over many.

## Run

```bash
make test            # pytest -q
make coverage        # pytest with coverage (html + term)
uv run pytest tests/unit/test_settings.py -q          # one file
uv run pytest -q -k "history or settings_card"        # one scenario
```

Determinism: fake sources use fixed timestamps (`make_mail`), test LLMs
return canned JSON, httpx and imaplib/smtplib are monkeypatched. No test
performs a real network request.

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
