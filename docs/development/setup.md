# Development

## Setup and local use

Use Python 3.11+, Git and [uv](https://docs.astral.sh/uv/).

```bash
make sync
cp configs/example.toml configs/local.toml
uv run mailflow config-check -c configs/local.toml
uv run mailflow tui -c configs/local.toml
```

`make sync` installs the locked workspace and development dependencies. On
Windows, `Copy-Item configs/example.toml configs/local.toml` replaces `cp` when
needed. Configure your own accounts/models only when testing live integrations;
normal tests use fakes and temporary data. The fake mail source is available for
offline development.

The `run`, `tui` and bot-export Make targets use `configs/development.toml`;
create that ignored local file from the example before using those targets.
Use explicit CLI `-c` paths for other configurations.

## Checks

Run `make help` for the current command list; [Makefile](../../Makefile) is the
source of truth.

| Command | Purpose |
| --- | --- |
| `make test-unit` | Isolated domain, component and transport-contract behavior |
| `make test-integration` | Real component composition, SQLite, ASGI and headless TUI behavior |
| `make test-e2e` | Actual CLI processes with temporary configuration and databases |
| `make test` / `make coverage` | Entire suite / coverage for current packages and plugins |
| `make validate-plugins` | Bundled-plugin contracts with simulated I/O |
| `make lint` / `make format-check` / `make format` | Lint / check formatting / apply formatting |
| `make typecheck` | Both mypy and strict pyright |
| `make check` | Tests, plugin contracts and all static checks |
| `make build` | Every workspace wheel under `dist/wheels` |
| `make clean` | Remove caches, reports and builds; keep environments and user data |

For focused work, run the affected tests first, then the relevant static checks.
Use full checks for changes across shared contracts or runtime boundaries.
Do not connect real mailboxes, models, markets or chat processes in automated
checks. A live diagnostic is a manual operation with explicit local credentials.

## Test boundaries

Keep tests small enough to prove an observable behavior. Preserve regressions
for mail identity/cursors, MIME/OCR, analysis fallback, task visibility,
notification retry, reply confirmation, permissions and remote conversion.
TUI tests should drive search, selection, refresh, cancellation or error recovery.

Do not lock down private call order, exact prompt wording, punctuation or mere
widget existence. Share fakes through `mailflow-testkit`; a chain of fakes is
not a real end-to-end test. Put composition tests in integration, and reserve e2e
for supported process entry points. Avoid fixed test-count claims in documentation.

## Packaging and export

Each workspace package has its own `pyproject.toml`; the core remains independent
of concrete plugins. `tools/build_all.py` is the wheel build list used locally
and in releases. Keep it aligned with workspace members and bundled registrations.

```bash
make build
make exe-standalone
# Smoke-test the standalone executable before building onefile.
make exe-onefile
```

Executable builds use Nuitka and explicit bundled imports. Compiler downloads
require an explicit `--download-tools` option. Arbitrary Python plugin discovery
is not supported by a frozen executable; include additional packages in the
build. Configuration paths and external language packs remain user-controlled.

Export a configured instance as a NoneBot or AstrBot plugin without starting a
mail runtime:

```bash
uv run mailflow export --framework nonebot --output dist/nonebot_plugin_mailflow -c configs/local.toml
uv run mailflow export --framework astrbot --output dist/astrbot_plugin_mailflow -c configs/local.toml
```

Install the generated package in the framework's environment and configure its
administrators. The generated host owns service startup/shutdown and must pass
platform identity through shared permissions. Exported packages include sensitive
configuration; keep them out of public repositories.

Release automation checks the tag against the core version and takes release
notes from [CHANGELOG.md](../../CHANGELOG.md). Plugin versions are independent,
but their package and runtime metadata must agree.

## Keeping both repositories aligned

Marketplace publishing and validation live in the
[plugin repository](https://github.com/Kingcxp/mailflow-repo). Use the main
[plugin guide](../plugin-development/overview.md) as the shared contract; do not
maintain a second interface definition there. When a plugin exists in both
repositories, update and validate both implementations together.

Keep local configuration, databases, logs, generated exports, reports and caches
ignored. `make clean` is the routine cleanup; the separately named `clean-*`
commands can modify user data and are not part of development verification.
