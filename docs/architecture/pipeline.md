# Processing pipeline

The pipeline owns execution semantics that Pluggy deliberately does not:
ordering, retries, timeouts and the failure policy.

## Bindings

Each configured processor becomes a `ProcessorBinding` (in `mailflow.pipeline`):

- `priority` — ascending order; equal priorities sort by processor id.
- `retries` — extra attempts after the initial one (0 = no retries).
- `timeout_seconds` — `asyncio.wait_for` bound on one `process()` call.
- `failure_policy` — `continue` (default: record a failed note, run the next
  processor) or `stop` (halt the chain).
- `llm` / `fallback_llms` — named LLMs routed through the `LLMRouter` for
  processors that need one (see `llm.md`).

## Execution

For each mail, in order:

1. Build a `ProcessingContext` (account id, timezone, processor options,
   injected `now` clock for determinism).
2. Run `process()` with retry/timeout; on success merge the returned
   `ProcessorResult.analysis` into the accumulated `MailAnalysis` (overlay
   wins per field; later processors override earlier ones).
3. Append a `ProcessorNote` (success/failed) with timestamps.
4. `ProcessorDecision.STOP` or a `stop`-policy failure halts the chain.

## Fallback-summary guarantee

If no processor produced a summary, the pipeline fills it from the subject
and appends a `pipeline` note — a mail is never stored without a summary.

## Result

`process()` returns `(analysis, notes, llm_used, llm_backend)`; the runtime
builds a `MailRecord` with `auto_urgency = analysis.urgency`, persists it,
emits `mailflow.mail.processed`, and runs notifiers whose threshold the effective
urgency meets.

## Failure isolation

A processor exception is captured in its note and logged; with
`failure_policy = continue` the next processor still runs. A mail that fails
processing entirely is logged by the worker and never kills the worker or
other sources.


## Summary language

The `llm-importance` processor writes summaries, notes and reply drafts in
the language given by its `language` option. `start_service` seeds it from
`general.summary_language` (when set) or the UI language; switching
`general.language` in the TUI hot-rebuilds the pipeline so the next
analysis follows immediately. An explicit per-processor `options.language`
entry always wins.
