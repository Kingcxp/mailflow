# Processor

The classification chain is built into `mailflow-core`: the deterministic
`rules` processor (priority 10) and the LLM `llm-importance` processor
(priority 20) run by default, so filtering and importance analysis work
with zero plugins. A plugin that registers the same component id replaces
the built-in step.

The plugin-facing extension point is the **LLM enhancer**: bounded
customization of the built-in LLM analysis without reimplementing
classification. This page documents both contracts.

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

## LLM enhancers (the processor-plugin extension point)

```python
@PLUGIN.llm_enhancer("my-enhancer")
class MyEnhancer:
    def __init__(self, config: ProcessorConfig) -> None:
        self._lang = str(config.options.get("lang", "zh-CN"))

    def system_prompt(self, base: str) -> str:
        return f"{base}\nSummaries must be written in {self._lang}."

    def extra_messages(self, mail: MailMessage, context: ProcessingContext) -> list[dict[str, str]]:
        return [{"role": "user", "content": "Be very concise."}]

    def post_process(
        self, analysis: MailAnalysis, mail: MailMessage, context: ProcessingContext
    ) -> MailAnalysis | None:
        if analysis.urgency == "low":
            return analysis.model_copy(update={"notes": "low priority: skipped digest"})
        return None
```

`system_prompt` results chain (the built-in prompt first, then each
enhancer), `extra_messages` are appended after the user message, and
`post_process` runs in order over the parsed analysis — returning `None`
leaves the analysis unchanged. Every hook is optional; an enhancer that
only appends guidance implements just `system_prompt`. Enhancers are
configured as ordinary processors:

```toml
[[processors]]
processor_id = "my-enhancer"
provider = "my-enhancer"
priority = 20
[processors.options]
lang = "zh-CN"
```

Use the `llm_enhancer` scaffold template to start one; `mailflow-core`
implements the aggregation, so enhancers never talk to the LLM router
directly.

## Writing a full processor (advanced)

The `MailProcessor` contract remains available for plugins that need a
custom step in the chain (e.g. domain-specific filtering) — it runs
alongside the built-in steps in priority order.

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
