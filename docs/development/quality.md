# Quality gates

Every gate is a Makefile target; the full gate is `make check`:

```bash
make lint           # ruff check (E,F,I,UP,B,SIM,ASYNC,RUF)
make format-check   # ruff format --check
make mypy           # mypy strict over packages/plugins/tools
make pyright        # pyright strict over packages/plugins/tests/tools
make typecheck      # mypy + pyright
make test           # pytest
make docs           # tools/check_docs.py (mandatory docs present)
make check          # lint + format-check + typecheck + test + docs
```

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
