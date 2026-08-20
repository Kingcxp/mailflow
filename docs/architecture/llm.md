# LLM routing and backends

## Named LLMs

Configuration declares named LLMs; each references a backend adapter
component (e.g. `openai-compatible`) plus its request configuration:

```toml
[[llms]]
llm_id = "go"                      # the name processors reference
provider = "openai-compatible"     # backend adapter component id
base_url = "https://relay.example/v1"
api_key = "${MAILFLOW_LLM_GO_TOKEN}"   # or api_key_env; optional
model = "deepseek-chat"
timeout_seconds = 60
max_retries = 2
default = true
fallback = ["local"]               # named llms tried after this one
headers = { ... }                  # merged into the request
query = { ... }
extra_body = { ... }
```

One backend instance is created per named LLM (each has its own endpoint,
model and credentials), keyed by `llm_id`.

## Routing

`LLMRouterImpl` (satisfies the `LLMRouter` protocol from `contracts.py`):

- `chat(messages, primary=..., fallback=[...], ...)` tries the primary named
  LLM, then fallbacks in order, de-duplicating repeated ids.
- Every completion is stamped with the named `llm_id` and the backend plugin
  id (`backend`) that actually served it; processors record both.
- If every backend fails, a single `LLMRouteError` aggregates sanitized
  per-backend messages.
- Configured API keys are redacted from aggregated error text (defense in
  depth on top of backend-level sanitization).

## OpenAI-compatible backend

`plugins/mailflow-llm-openai-compatible` POSTs to `base_url + path`
(default `chat/completions`) with:

- Bearer auth **only** when a token is configured; user headers win via
  `setdefault`.
- Config `headers`/`query`/`extra_body` merged with per-call `options`
  (per-call wins for body/model/temperature; per-call headers/query merge).
- Bounded exponential-backoff retries (`max_retries`, capped).
- `choices[0].message.content` parsed into `LLMCompletion`, including the
  list-of-parts content form some endpoints return.

**Secrets**: raised error text never contains the request URL or query
(strings may carry credentials); the router additionally redacts keys.

## Anthropic backend

`plugins/mailflow-llm-anthropic` (`provider = "anthropic"`) speaks the
Messages API: the system prompt travels in the top-level `system` field, the
remaining messages keep their roles, and the key goes in `x-api-key` next to
`anthropic-version: 2023-06-01`. `[llms.options]` adds `base_url` (a full
messages endpoint, not a prefix) and `max_tokens` (default 1024, required by
the API). Three attempts with exponential backoff; any error text containing a
URL is reduced to `transport error` before it can reach a persisted note.

## Built-in processors

`mailflow/processors.py` (registered under plugin id `mailflow-core`, **not**
a plugin) holds both defaults:

- `rules` — deterministic pre-filter: advertising keywords (word-boundary
  matches) and an important-senders allowlist, so obvious junk never reaches
  the model.
- `llm-importance` — prompts with the exact four-level semantics, injects the
  mail content, current time, timezone and the rolling feedback guidelines,
  and parses a structured JSON answer (summary, urgency, reason,
  reply_required, suggested_reply, action_items with due windows and
  preparation notes). Fenced or prose-wrapped JSON is tolerated; urgency
  synonyms and case variants are normalized; action items carry a `mail_id`
  backlink and timezone-aware dates. The result records which backend/LLM
  actually served the request.

The default chain when `[[processors]]` is absent is `rules` at priority 10
and `llm-importance` at 20. `general.summary_language` (or the interface
language) is injected as the output language unless the processor's
`options.language` already sets one.

## Extending the analysis: LLMEnhancer

A plugin may register an `LLMEnhancer` (`add_llm_enhancer`) instead of
replacing the processor. Three optional hooks — `system_prompt(base)`,
`extra_messages(mail, context)` and `post_process(analysis, mail, context)` —
let it extend the prompt and adjust the parsed result within the same
four-level contract. Enhancers are active as soon as they are installed; an
explicit `[[processors]]` section with `enabled = false` for that component id
turns one off.
