# Tests

## Boundaries

| Directory | Scope |
|---|---|
| `tests/unit` | One component or domain rule, with transport and provider doubles |
| `tests/integration` | Real component composition, SQLite, ASGI, headless TUI and source plugin validation |
| `tests/e2e` | Real CLI subprocesses using TOML, built-in fake mail sources and SQLite across restarts |

Shared doubles live under `tests/mailflow_test_support`; collected test modules do not import one another.
Assertions should prove visible results, stored state, protocol data or resource cleanup.
Avoid fixed prompt wording, widget-presence checks, private call sequences and machine-speed thresholds.
Give each behavior one primary test boundary; higher layers should verify integration rather than
repeat every lower-layer case. Share setup for a coherent workflow, while keeping independent
failure and concurrency regressions. Count collected cases as well as test functions when reviewing
the suite; parameterization should express meaningful input differences.

## Run

```bash
make test
make test-unit
make test-integration
make test-e2e
make validate-plugins
make coverage
```

`make coverage` reads directory sources from `pyproject.toml`, including every current
workspace package and plugin. Coverage also follows CLI subprocesses. HTML and XML
reports go under `build/reports/`; `make clean` removes reports and coverage data.
Use `uv run --frozen --all-packages pytest --collect-only -q` for the current case count.

## Offline verification

Mailbox, model, OCR service, chat platform and marketplace transports are replaced.
The shared `mailflow_testkit.plugin_smoke` runner rejects unmocked network/process I/O
and checks signatures and sample results through `mailflow-core` contracts.
The companion repository's validator invokes this same runner from the paired core/testkit checkout.

Optional local OCR recognition skips when its engine is absent. Creating filesystem
links may require an OS permission. The NoneBot TOML and resolved-seminar regressions
run as ordinary passing tests. The composed service test follows source ingestion,
analysis, SQLite persistence and notification delivery into REST and the remote TUI.
Legacy database checks reopen existing rows without rewriting them or removing custom tables.
