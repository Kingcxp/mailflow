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

## LLM importance processor

`plugins/mailflow-processor-llm-importance` prompts with the exact four-level
semantics, injects the mail content, current time and timezone, and parses a
structured JSON answer (summary, urgency, reason, reply_required,
suggested_reply, action_items with due windows and preparation notes).
Fenced or prose-wrapped JSON is tolerated; urgency synonyms and case
variants are normalized; action items carry a `mail_id` backlink and
timezone-aware dates. The result records which backend/LLM actually served
the request.
