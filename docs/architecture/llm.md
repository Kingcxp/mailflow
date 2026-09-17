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
timeout_seconds = 120
max_retries = 2
default = true
fallback = ["local"]               # named llms tried after this one
headers = { ... }                  # merged into the request
query = { ... }
extra_body = { ... }
```

One backend instance is created per named LLM (each has its own endpoint,
model and credentials), keyed by `llm_id`.

`timeout_seconds` bounds one HTTP request and defaults to **120 s**: it has to
cover the wait for the first token as well as the whole answer, and a cold or
slow local model regularly needs more than the 60 s this used to default to.
Raise it (and the owning `[[processors]] timeout_seconds`) for an endpoint that
thinks for a long time before answering; the Settings UI edits both.

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

- `rules` — deterministic hints before any LLM work; **never a verdict**. A
  promotional keyword hit contributes an `ad` overlay (the value that survives
  if the model call fails) and the chain continues, so the model can and does
  overrule it. A single strong word (`promotion`, `sale`, `discount`,
  `advertisement`, `limited offer`, `act now`) is enough to hint; the
  mailing-list boilerplate (`unsubscribe`, `click here`) that every bulk mail —
  university notices, lecture invitations, newsletters — carries in its footer
  needs two independent hits. The old rule stopped the chain on one hit, which
  quietly classified exactly that mail as `ad` without ever analysing it.
  An important-senders allowlist contributes `important` and also continues.
- `llm-importance` — prompts with the exact four-level semantics, injects the
  mail content, current time, timezone, the recipient profile and the rolling
  feedback notes, and parses a structured JSON answer (summary, urgency,
  reason, reply_required, suggested_reply, action_items with due windows and
  preparation notes). Fenced or prose-wrapped JSON is tolerated; urgency
  synonyms — including the Chinese level names a model may translate to — and
  case variants are normalized; action items carry a `mail_id` backlink and
  timezone-aware dates. The result records which backend/LLM actually served
  the request.

  The prompt's level contract is load-bearing and test-enforced: mail is judged
  by **what the recipient must do** — anything to act on, answer or track is at
  least `important`, `info` is for optional/FYI mail, and `ad` is reserved for
  unusable mail (bulk, automated or mass-mailed is explicitly *not* a reason to
  choose `info` or `ad`). Feedback notes are rendered as narrow, kind-scoped
  preferences with repeats collapsed (a store polluted by twenty identical
  rejections used to read as an absolute rule), and they may never override a
  deadline or a required action.

A processor's `fallback_llms` is kept in sync with the configured chain:
adding a second LLM makes the model reachable as a fallback, and deleting one
drops it from the list (an explicitly configured fallback list is preserved).
Without that, a failing primary simply failed the analysis instead of routing
to the next named LLM.

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
