# Configuration

## Loading

`load_config(path)` (in `mailflow/config.py`) reads TOML, expands
whole-string `${ENV_VAR}` placeholders recursively, and validates. An unset
referenced variable is a hard error; `prefix-${VAR}-suffix` stays literal.
Without a path, built-in defaults are used.

## Groups (`MailFlowConfig`)

| Group | Purpose | Key defaults |
| ----- | ------- | ------------ |
| `[general]` | language, timezone, retention, cleanup time, queue/workers | en, UTC, 30-day retention, 7-day trash, 04:00 cleanup, queue 500, workers 2 |
| `[logging]` | console/file/jsonl sinks, levels, per-logger levels, console redirect | console+file on, INFO |
| `[plugins]` | `enabled` (allowlist when non-empty), `disabled` | none |
| `[storage]` | provider component id, db path | `sqlite`, `data/mailflow.db` |
| `[[accounts]]` | account id, source provider, email, options | — |
| `[[llms]]` | named LLM: provider, base_url, api_key/api_key_env, model, headers/query/extra_body, timeouts, retries, default, fallback | provider `openai-compatible` |
| `[[processors]]` | provider, priority, llm + fallback_llms, failure_policy, retries, timeout, options | — |
| `[[notifiers]]` | provider, enabled, minimum_urgency | — |
| `[i18n]` | language, extra pack directories | `en` |

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
(`fake`, `sqlite`, `openai-compatible`, `rules`, `llm-importance`,
`console`), not package names. `plugin list` shows the mapping.

## Secrets

Inline tokens may use whole-string env placeholders; alternatively
`api_key_env = "NAME"` reads the environment at validation. Never commit
tokens — `configs/local.toml` is gitignored.
