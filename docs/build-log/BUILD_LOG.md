# Build log — MailFlow v0.1.0 reconstruction

Honest record of what was actually executed and verified while rebuilding
MailFlow from `MAILFLOW_FROM_ZERO.md` (stage by stage, one commit per stage).
Every commit passed `git diff --check`; gates were re-run after each stage.

Environment: Windows 11, Python 3.11.4, uv 0.11.2, git 2.55. The workspace
resolved to Python 3.11 (tzdata added as a Core Windows dependency so
`ZoneInfo` works).

## Stage log

| Stage | Commit | Executed verification |
| ----- | ------ | --------------------- |
| 00 bootstrap | `chore: bootstrap uv workspace...` | `uv lock`, `compileall tools` |
| 01 domain | `feat(core): define normalized mail...` | `pytest tests/unit/test_core.py` (15) |
| 02 config | `feat(core): add typed runtime configuration...` | unit tests (30) |
| 03 contracts/registry | `feat(core): introduce typed component contracts...` | `compileall`, `ruff check` |
| 04 pluggy | `feat(core): add entry-point plugin discovery...` | `mypy packages/mailflow-core`, `pyright` |
| 05 events/llm/pipeline | `feat(core): add event bus, llm fallback routing...` | unit tests (48), mypy, pyright strict |
| 06 logging | `feat(core): add queue-based rich logging...` | unit tests (52) |
| 07 i18n | `feat(core): add english chinese and external json localization...` | unit tests (63) |
| 08 runtime | `feat(core): add bounded async runtime...` | unit tests (73) |
| 09 service | `feat(core): expose embeddable service facade...` | unit tests (84) |
| 10 commands | `feat(core): add transport-neutral management command router...` | unit tests (97) |
| 11 testkit/fake | `test: add reusable fake mail llm and notifier components...` | mypy+pyright over packages/plugins, unit tests |
| 12 sqlite | `feat(storage): add sqlite persistence...` | integration tests (11) |
| 13 openai backend | `feat(llm): add configurable openai-compatible...` | integration tests (15) |
| 14 processors | `feat(processors): classify mail urgency replies...` | unit tests incl. critical exam |
| 15 notifier + e2e | `test: cover public service startup...` | full suite (124) |
| 16 bundled + cli | `feat(cli): add rich standalone host...` | full suite (126); `mailflow --help`, `command help`, `doctor`, `snapshot --json` |
| 17 tui | `feat(tui): add mail action runtime log and settings interface...` | full suite (127) incl. headless TUI pilot |
| 18 configs/i18n | `docs(config): add complete runtime...` | `config-check` on both configs; `mail list/show`, `lang set zh-CN` against dev config |
| 19 build/exe | `build: add workspace wheel and nuitka...` | `uv build --all-packages` (11 wheels); Nuitka standalone compiled 909 C files, exe verified: `command help`, `config-check`, `mail list` |
| 20 docs | `docs: document mailflow architecture...` | `make docs`, `make check` |

## Final gates (executed, not assumed)

```bash
uv run ruff check .            # pass
uv run ruff format --check .   # pass
uv run mypy packages plugins tools   # pass (strict)
uv run pyright                 # pass (strict; CLI file uses # pyright: basic)
uv run pytest -q               # 127 passed
uv run python tools/check_docs.py  # pass
uv build --all-packages        # pass (11 wheels)
```

## Manual smoke tests (executed)

- `uv run mailflow --help`, `command "help"`, `doctor`, `snapshot --json`
- `command "mail list"` / `mail show` / `lang set zh-CN` with
  `configs/development.toml` (LLM endpoint absent → graceful failure notes,
  rules result kept)
- frozen `dist/frozen_entry.dist/frozen_entry.exe`: `command "help"`,
  `config-check -c configs/development.toml`, `command "mail list"` — all
  pass; locale JSON + app.tcss bundled.

## Not executed

- Real provider integrations (IMAP/Gmail/Outlook) — explicitly out of
  scope for the 0.1.0 baseline (planned provider phase).
- Network LLM calls in tests: never performed (httpx monkeypatched).

## Phase 2 (post-tag review & features)

| Commit | Change | Executed verification |
| ------ | ------ | --------------------- |
| `fix(core): retry storage saves...` | save retries, broader secret redaction, coverage for all packages | `make coverage` (86%), full suite |
| `feat(core): remind on timed actions...` | early + day-of reminders, persisted fired state | reminder unit tests (17 runtime tests) |
| `feat(core): manage every config option...` | config list/get/set, TUI config table, comment-preserving TOML patching | `config set` CLI smoke on a commented file (comments preserved) |
| `feat(core): plugin marketplace...` | repo/market/install commands, TUI Market tab, `mailflow-plugins` sibling repo | end-to-end install of the webhook sample via `uv pip`; uninstalled after |

Final phase-2 gate: 144 tests passed, mypy/pyright strict clean, ruff clean.

## Executed after the initial log

- `make exe-onefile` equivalent: `tools/build_exe.py --mode onefile`
  completed; `dist/frozen_entry.exe` verified (`plugin list`,
  `config-check -c configs/development.toml`).
- `uv run mailflow tui -c configs/development.toml` launched and rendered
  (header, tabs, search placeholder) in this environment.
- Interactive `mailflow shell` pipelined `help mail`, `mail list`,
  `lang get`, `exit` successfully.
