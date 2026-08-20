# Configuration

## Loading

`load_config(path)` (in `mailflow/config.py`) reads TOML, expands
whole-string `${ENV_VAR}` placeholders recursively, and validates. An unset
referenced variable is a hard error; `prefix-${VAR}-suffix` stays literal.
Without a path, built-in defaults are used.

## Groups (`MailFlowConfig`)

| Group | Purpose | Key defaults |
| ----- | ------- | ------------ |
| `[general]` | language, timezone, retention, cleanup time, reminders, queue/workers | en, UTC, 30-day retention, 7-day trash, 04:00 cleanup, early reminder 2 days before at 08:00 + 00:00 on the due day, queue 500, workers 2 |
| `[logging]` | console/file/jsonl sinks, levels, per-logger levels, console redirect | console+file on, INFO |
| `[plugins]` | `enabled` (allowlist when non-empty), `disabled`, `repositories` (marketplace name+url pairs) | none |
| `[storage]` | provider component id, db path | `sqlite`, `data/mailflow.db` |
| `[[accounts]]` | account id, source provider, email, options | — |
| `[[llms]]` | named LLM: provider, base_url, api_key/api_key_env, model, headers/query/extra_body, timeouts, retries, default, fallback | provider `openai-compatible` |
| `[[processors]]` | provider, priority, llm + fallback_llms, failure_policy, retries, timeout, options | — |
| `[[notifiers]]` | provider, enabled, minimum_urgency | — |
| `[i18n]` | language, extra pack directories | `en` |

## Inspecting and changing options

`config list [group]`, `config get <key>` and `config set <key> <value>` show
every option with its type, required/optional marker, default, description
and current value; secret fields (`api_key`, token-like headers) are
redacted. `config set` coerces the value, validates the whole config, and
patches the TOML file in place (comments preserved; full rewrite when the key
or its section is absent).

## The settings editor (`mailflow/settings.py`)

`mailflow.settings` turns the typed schema into an editor-shaped surface that
every host shares — the TUI Settings tab is a thin client of it:

| Function | Purpose |
| -------- | ------- |
| `build_sections(config, registry=, plugin_titles=)` | Sidebar model: MailFlow's own sections (`general`, `logging`, `plugins`, `storage`, `i18n`) plus one section per plugin that owns a configured component. `accounts`/`llms` are excluded — they have dedicated tabs. |
| `find_spec(config, key)` | One `OptionSpec`: editor kind, description, schema default, current value, required flag, choices. |
| `apply_value(config, key, raw)` | Coerce, re-validate the whole config, return the updated copy. |
| `reset_value(config, key)` | Restore one option to its schema default. |
| `add_entry` / `update_entry` / `remove_entry` / `move_entry` | Edit `accounts`, `llms`, `processors`, `notifiers` entries. |
| `normalize_llm_chain(config)` | Re-derive `default`/`fallback` from the LLM list order. |

`EditorKind` is derived from the pydantic field type, so a host never
hard-codes which widget an option needs: `text`, `secret`, `integer`,
`number`, `boolean`, `choice` (enums), `string_list`, `mapping`,
`struct_list`, `struct`.

Every mutation raises `SettingsError` carrying the offending option key and a
readable reason (type mismatch, range violation, or a model validator such as
the timezone check), so a UI can point at the field that is wrong. Nothing is
persisted unless the resulting config re-validates as a whole.

### LLM order is the fallback chain

For `[[llms]]` the list order *is* the routing policy: the first entry is the
default and every entry falls back to the ones after it. `move_entry` and
`normalize_llm_chain` rewrite `default`/`fallback` accordingly, so those two
fields are derived, never hand-maintained. Removing an LLM also scrubs it from
every other entry's `fallback` and from processors that referenced it, which
keeps cross-reference validation satisfiable.

## Validation

- Timezone must be a valid IANA name (`ZoneInfo`); `tzdata` is a Windows
  dependency of Core.
- Log levels must be valid Python levels.
- Named LLM references: `default` at most once; every `fallback` id must
  exist; every processor `llm`/`fallback_llms` must exist; a processor
  cannot have fallbacks without a primary.
- `enabled ∩ disabled` plugin lists must be empty.

## Adapter ids vs package names

Config `provider` fields reference **component ids** registered by plugins
(`imap`, `fake`, `sqlite`, `openai-compatible`, `anthropic`, `rules`,
`llm-importance`, `console`), not package names. `plugin list` shows the
mapping.

## Secrets

Inline tokens may use whole-string env placeholders; alternatively
`api_key_env = "NAME"` reads the environment at validation. Never commit
tokens — `configs/local.toml` is gitignored.

Writing the config back never materializes a resolved secret:
`load_config` records which paths came from a `${VAR}` placeholder in
`MailFlowConfig.env_placeholders` (excluded from serialization), and
`write_config` restores the placeholder. An `api_key` resolved from
`api_key_env` is written back empty, because the loader resolves it again on
the next start. Rotating a credential therefore means changing the
environment variable only.
