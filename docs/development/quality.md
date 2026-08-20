# Quality gates

Every gate is a Makefile target; the full gate is `make check`:

```bash
make lint           # ruff check (E,F,I,UP,B,SIM,ASYNC,RUF)
make format-check   # ruff format --check
make mypy           # mypy strict over packages/plugins/tools
make pyright        # pyright strict over packages/plugins/tests/tools
make typecheck      # mypy + pyright
make test           # pytest
make docs           # tools/check_docs.py (docs present AND accurate)
make check          # lint + format-check + typecheck + test + docs
```

## The docs gate

`tools/check_docs.py` does two jobs. First it asserts the 37 mandatory
documents exist. Then it cross-checks every prose document against the code,
because presence alone let stale docs rot silently:

| Check | Fails when a doc… |
| ----- | ----------------- |
| paths | backtick-quotes a `packages/…`, `plugins/…`, `tests/…`, `tools/…` or `translations/…` path that does not exist |
| make targets | shows a `make <target>` command that the Makefile does not define |
| event names | quotes a `mailflow.*` event that no `events.emit(...)` call in core produces |
| service methods | writes `service.<name>(` for a method `MailFlowService` does not have |
| plugin ids | quotes a `mailflow-*` id that is not a real package or plugin (a directory needs a `pyproject.toml`, or a `plugin.json` in the marketplace checkout, so a leftover `__pycache__` folder cannot keep a deleted plugin "alive") |

`CHANGELOG.md` and `docs/build-log/BUILD_LOG.md` are exempt from the accuracy
checks: they are historical records and legitimately name things that were
later removed. Marketplace plugin ids are only verified when the sibling
`mailflow-repo` checkout is present, so CI without it stays green.

Locale hygiene is enforced by tests instead (`tests/unit/test_i18n.py`):
en/zh-CN key parity, no duplicate lookup paths, no Chinese text in `en.json`,
and a `config.desc.<key>` entry for every configurable option.

## Configuration

Root `pyproject.toml` centralizes the toolchain:

- `[tool.ruff]` — target py311, line length 100, `src` covers packages/
  plugins/tests/tools; fullwidth parens allowed for CJK strings.
- `[tool.mypy]` — `strict = true`; `mypy_path` makes every workspace package
  resolve as first-party source.
- `[tool.pyright]` — `strict`; `extraPaths` for the same reason; the CLI glue
  file uses `# pyright: basic` because Typer's decorator stubs are too loose
  for strict mode (it carries no business logic).
- `[tool.pytest.ini_options]` — `asyncio_mode = "auto"`.

## Known friction points

- Pydantic fields with `Field(default_factory=list)` infer as
  `list[Unknown]` under pyright strict with pydantic ≥ 2.11; use lambda
  factories (`Field(default_factory=lambda: [])`) to keep both checkers
  happy.
- `isinstance` narrowing of `Any` to `dict[Unknown, Unknown]` annoys pyright
  strict; cast at the call site or type the helper parameter as
  `dict[Any, Any]`.
- Textual widgets are loosely typed; `query_one` with a subscripted generic
  fails at runtime (isinstance), so cast after the lookup with an explicit
  `# pyright: ignore` where needed.

## Coverage

`make coverage` reports term + html; the baseline suite covers domain
contracts, config validation, logging isolation/redaction, pipeline
semantics, storage trash behavior, LLM transport (monkeypatched), the reply
state machine and the public E2E flow.
