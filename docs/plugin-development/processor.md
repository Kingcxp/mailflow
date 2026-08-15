# Processor

A processor is one step in the ordered chain. It sees the original mail and
returns a partial analysis plus a decision.

## Contract

```python
class MailProcessor(Protocol):
    processor_id: str
    async def process(self, mail: MailMessage, context: ProcessingContext) -> ProcessorResult: ...
```

`ProcessingContext` carries `account_id`, `timezone`, the processor
`options` from configuration, and an injected `now` clock (use it for
determinism instead of `datetime.now()`).

`ProcessorResult`:

- `analysis: MailAnalysis | None` — a partial overlay; the pipeline merges
  it into the accumulated analysis (non-empty fields win, urgency and the
  reply flag always come from the overlay).
- `decision` — `continue` (default) or `stop` (halt the chain).
- `llm_used` / `llm_backend` — set when an LLM actually served the request.
- `notes` — human-readable trail entries appended to the processor note.

## Registration

```python
def build_processor(config: ProcessorConfig, router: LLMRouter) -> MyProcessor:
    return MyProcessor(config.options, router)

registrar.add_processor("my-processor", build_processor)
```

```toml
[[processors]]
processor_id = "my-processor"
provider = "my-processor"     # component id
priority = 30                 # ascending execution order
retries = 1
timeout_seconds = 10
failure_policy = "continue"   # continue | stop
llm = "go"                    # optional named LLM
fallback_llms = ["local"]
[processors.options]          # read from context.options
```

## Using the LLM router

```python
completion = await router.chat(
    [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}],
    primary=self._llm_id,
    fallback=self._fallbacks,
    options={"temperature": 0.2},
)
# completion.llm_id / completion.backend tell you what actually served it
```

Only registered named LLMs may be referenced; unknown ids fail fast at
config validation.

## Rules for authors

- Cheap deterministic work first (keywords, sender lists) before LLM calls.
- Never raise for a merely odd mail — return a conservative analysis
  (`info`) and let `failure_policy` handle real errors.
- Sanitize any error text you put into results; the pipeline also truncates
  and strips credential-like fragments from persisted notes.
- If your processor produces timed obligations, return `ActionItem`s with
  timezone-aware `due_at`/`due_end` and a `mail_id` backlink.
