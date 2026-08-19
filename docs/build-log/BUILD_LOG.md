# Build log — MailFlow v0.1.0 reconstruction

Honest record of what was actually executed and verified while rebuilding
MailFlow stage by stage (one commit per stage). Every commit passed
`git diff --check`; gates were re-run after each stage.

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
| `feat(core): plugin marketplace...` | repo/market/install commands, TUI Market tab, `mailflow-repo` sibling repo | end-to-end install of the webhook sample via `uv pip`; uninstalled after |

Final phase-2 gate: 144 tests passed, mypy/pyright strict clean, ruff clean.

## Executed after the initial log

- `make exe-onefile` equivalent: `tools/build_exe.py --mode onefile`
  completed; `dist/frozen_entry.exe` verified (`plugin list`,
  `config-check -c configs/development.toml`).
- `uv run mailflow tui -c configs/development.toml` launched and rendered
  (header, tabs, search placeholder) in this environment.
- Interactive `mailflow shell` pipelined `help mail`, `mail list`,
  `lang get`, `exit` successfully.

## Marketplace v2 round (post v0.2.0)

| Change | Verification |
| ------ | ------------ |
| Per-plugin folder layout: root `index.json` (categories only) + `category/<id>/plugin.json` (markdown readme) + source; `_list_plugin_dirs` over file://, GitHub contents API, INDEX.json fallback | CLI e2e: `repo add` local + `market list` (5) + `market show`; unit/e2e fixtures migrated; 152 tests |
| Plugin i18n: `MarketPlugin.descriptions` / `readmes`; `description_for` / `readme_for` pick the active app language (CLI list/show/search + TUI table/detail); search matches translations | localization tests (zh-CN readme/description, en fallback) |
| Rich markdown spans: render via rich Markdown with `<span style="color:…">` support (sentinel markers → span styles), bold/strike/code/quote/bullets/headings; bounded and merged with overlapping styles | span-color + boundedness unit tests |
| Plugin template generator (`mailflow.plugin_template`): 5 category stubs producing complete loadable plugins | 14 template tests; e2e: scaffold → temp venv install → entry-point discovery → `build_registry` instantiation → processor `process()` ran |
| TUI new-plugin wizard (`PluginScaffoldScreen`): DirectoryTree folder pick + subfolder checkbox/name + template-category Select | headless pilot test scaffolds into tmp_path |
| `mailflow-repo` documentation: docs/ (getting started, metadata ref, categories, per-category guides, localization, validation), per-category README + INDEX.json, per-plugin README, README links | repo pushed; validator `--all` passes 5/5 |
| Plugin PR validation workflow (`validate-plugins.yml`) + `tools/validate_plugin.py`: changed-plugin detection (unchanged plugins skipped), metadata/install/entry-point/registration checks + real processor run | local run of the script against all 5 plugins: `ok: 5 plugin(s) valid, loadable and runnable` |
| MailFlow CI workflow (`ci.yml`): uv sync, ruff check/format, mypy, pyright, pytest, docs gate on push + PR (ubuntu + windows) | workflow committed |

## Bot-export round (post v0.2.0)

| Change | Verification |
| ------ | ------------ |
| `BOT_EXPORTER` component kind + `registrar.add_bot_exporter`/`bot_exporter_factory`; `mailflow.bot_export` (`BotExportContext`/`BotExportResult`, `available_frameworks`, `export_bot_plugin`) | pyright/mypy strict clean; unit tests (registry + export + exporters) |
| `plugins/mailflow-export-nonebot` + `plugins/mailflow-export-astrbot` (framework plugin generators embedding resolved config + deps) | workspace smoke: both frameworks export and config round-trips; scratch-venv install → entry-point discovery → factory instantiation OK |
| `bot_exporter` scaffold category in `mailflow.plugin_template` (wizard + template) | scaffolded module compiles, imports, reports BOT_EXPORTER kind, registers framework id |
| CLI `mailflow export --framework <id> --output <dir>` + `make bot-plugin*` targets | CLI export runs end-to-end on the dev config |
| TUI `BotExportScreen` wizard (framework select + DirectoryTree + subfolder) wired to the Market tab Export button | headless pilot e2e test exports into tmp_path |
| `mailflow-repo`: `bot_exporter` category (2 plugins), docs/bot-exporter.md, validator support | `validate_plugin.py --all` passes 5/5 existing + 2/2 new (against local core; scratch-env core install requires the pushed core) |
| README.zh-CN.md + README bot-export docs; MAILFLOW_FROM_ZERO.md removed, references cleaned | docs gate OK (35 mandatory documents) |

## User action items round (post bot-export round)

| Change | Verification |
| ------ | ------------ |
| `action add|delete` commands (summary + --due/--type/--notes, local-zone parsing) and `service.add_action/delete_action`; `list_actions` merges mail-derived + user items sorted by due time | command unit tests (add/list/show/delete, validation, mail-derived not deletable) |
| StorageBackend protocol + SQLite `custom_actions` table + MemoryStorage/FakeStorage fakes | sqlite roundtrip/overwrite/delete + restart-persistence tests |
| Reminder scheduler fires for user items too (`_fire_reminder` refactor; record=None payload) | runtime test: user item fires once, record is None, no re-fire |
| i18n keys (en/zh-CN) for add/delete/source-user; README commands rows | i18n parity test green |

## Letter replies + formatting round (post user-action round)

| Change | Verification |
| ------ | ------------ |
| `mailflow.letters`: cn/en formal-letter templates (auto date, right-aligned signature block), lightweight markup dialect (`**bold** *italic* <right> <center>`, blank-line paragraphs), `html_to_text` | 13 unit tests (structure, skeleton, escaping, round-trip, passthrough, alignment) |
| `service.create_letter_draft(mail_id, language)` (config-timezone today); commands `reply compose <id> <cn\|en>`, `reply edit` markup conversion, `reply show` plain text | command tests: compose cn/en + unknown template, edit markup, show without tags |
| TUI ReplyModal: template buttons + format toolbar (bold/italic/left/center/right via TextArea selection/line ops), dialog layout in app.tcss | e2e: apply cn template (structure + right align + date), bold selection, align line, save persists |
| `plugins/mailflow-notify-telegram` + marketplace copy, notifier docs/INDEX/README | integration test (urllib monkeypatch: URL/body, skip-without-credentials); `validate_plugin.py` ok 1/1 |
| i18n keys (reply.*, tui.reply_*) en/zh-CN; README command rows; changelog | i18n parity test green |

## Chat/updates/template round (post letter round)

| Change | Verification |
| ------ | ------------ |
| Account-independent mail identity + in-flight/storage dedup in the runtime | dedup tests: cross-account forward stored/notified once, refetch skipped, digest identity |
| Paginated chat listings (mail/action/plugin/help, 10/page), --query, unique-prefix ids, wrap-friendly rows | command tests: pagination/query/prefix |
| `feedback` command → rolling guidelines injected into LLM analysis (ProcessingContext) | pipeline injection tests + command tests |
| Daily 08:00 digest event (today/upcoming counts + items) once per day | runtime digest tests (fires once, pre-hour silent) |
| `mailflow.updates`: GitHub release check, plugin marketplace version check, update-source gating, `update` commands, daily auto-update loop, `general.auto_update` | updates unit tests (release/plugin/local-source gating/commands/loop) |
| Local plugin installs: `plugin install <path>` + TUI file-tree installer (detect_plugin_folders) | command tests single/batch/empty |
| Chat bridge in exported nonebot/astrbot templates (mailflow prefix, chunked replies, digest paging); marketplace copies synced | templates compile-checked; export tests |
| `mailflow.plugin_api` declarative decorators; scaffold templates generate this style with dev dependency group + uv install docs | plugin_api tests; all six template categories compile/load/register |
| docs/development/deployment.md (3 platforms) + overview decorator style; docs gate now 37 | docs gate OK |

## Processor rework + built-in mail/LLM plugins round

| Change | Verification |
| ------ | ------------ |
| Core classification built in: `mailflow.processors` (RulesProcessor, LLMImportanceProcessor, `register_builtin_processors`, enhancer aggregation); default chain `rules@10, llm-importance@20`; `LLMEnhancer` protocol + `ComponentKind.LLM_ENHANCER` + `add_llm_enhancer`/`llm_enhancer` decorator | make check green (259 tests); template factories instantiate for all 7 categories |
| Removed `mailflow-processor-rules` / `mailflow-processor-llm-importance` plugins; bundled/pyroot deps, uv.sources, mypy/pyright paths, tests migrated | make check green (plugin_id now "mailflow-core") |
| `plugins/mailflow-llm-anthropic`: Messages API backend (x-api-key, sanitized errors, bounded retries, `_parse` content blocks) | pyright 0 errors, ruff clean, mypy clean; FakeClient test verifies system/messages payload |
| `plugins/mailflow-mail-imap`: stdlib imaplib/smtplib, presets qq/163/outlook/gmail/generic, MIME→MailMessage pure `parse_mime`, SMTP replies, per-poll dedup | integration tests: MIME text/html, SMTP login/send, IMAP poll + dedup; make check 264 tests |
| Both plugins registered in `mailflow-bundled` + workspace (uv.sources, mypy_path, pyright extraPaths); TestBundledRegistration updated | make check green |
| Marketplace: mail_source/mailflow-mail-imap + llm_backend/mailflow-llm-anthropic copies, llm_enhancer category, processor docs, README/INDEX; validator now installs declared third-party deps | `validate_plugin.py --all` ok 10/10 |
| Docs: deployment (presets table + anthropic config), plugin overview/processor (LLMEnhancer), plugin-system (LLM_ENHANCER kind, 8 bundled plugins), README en/zh-CN, CHANGELOG | docs gate OK |
## TUI fixes round (post market round)

| Change | Verification |
| ------ | ------------ |
| Textual 8 button regression: `height: 3` + tall border overflowed 3-row containers (labels collapsed to 0), and the stock variant palette draws on ansi_default (black-on-black). Flat high-contrast variants (`$primary/$success/$warning/$error/$surface-lighten-1` + dark text) with `height: auto`; container heights relaxed | probe: all buttons render labels, height > 0; `test_market_buttons_rendered_with_labels` |
| Full-screen plugin detail modal (title, metadata author/updated/homepage, markdown readme, install/uninstall/enable/disable) on row select; row highlight previews in the pane | `test_market_detail_shows_author_and_updated` (modal assertion) |
| Config edit modal: select a config row → form (value + cancel/save) → `service.set_config_value` persists and table refreshes; list-valued options show a TOML hint | `test_config_edit_modal_saves_value` (timezone → Asia/Shanghai persisted) |
| `general.summary_language`: pins LLM summary/reason/reply language; empty follows the interface language; injected into the built-in llm-importance user message | `TestSummaryLanguage` (instruction injected / absent without config) |
| Config descriptions localized via `config.desc.<key>` keys (en + zh-CN) with english fallback | TUI e2e checks description renders (not the raw key) |
| `cursor_type: row` on every DataTable — row selection events never fired under the default `cell` cursor | all table interaction e2e green |
| make check full suite | 268 tests + docs gate OK |
