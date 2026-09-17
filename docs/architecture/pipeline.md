# Processing pipeline

The pipeline owns execution semantics that Pluggy deliberately does not:
ordering, retries, timeouts and the failure policy.

## Bindings

Each configured processor becomes a `ProcessorBinding` (in `mailflow.pipeline`):

- `priority` — ascending order; equal priorities sort by processor id.
- `retries` — extra attempts after the initial one (0 = no retries).
- `timeout_seconds` — `asyncio.wait_for` bound on one `process()` call,
  **120 s by default**. It must cover the model's first-token latency *and* the
  whole answer: 30 s made cold or slow local models time out on every mail,
  which then fell back to a subject summary with a failed note. An expired
  deadline is recorded as `processor timed out after N seconds`, never as an
  empty failure reason. The per-request budget is `[[llms]] timeout_seconds`
  (also 120 s by default) — raise both for a slow endpoint.
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

## Re-analysis

The store upserts by record id, so a re-analysis normally replaces the stored
record with the fresh result. The exception is a **failed** run — the model
timed out, was rate-limited or answered unusably, i.e. the result is a
subject fallback or carries a failed processor note. When the mail already had
a completed analysis, that previous analysis (summary, reason, urgency, action
items and any manual override) is kept and the new failure is appended to the
note trail, so the mail reads "shows the last good analysis, the latest attempt
failed" instead of regressing to its subject. The on-demand path reports those
notes back to its caller, which is how the TUI counts such a mail as failed
rather than as a success. There is nothing to keep on a first analysis, so the
failure is stored with its fallback summary as before.

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
`general.summary_language` (when set) or the UI language; the persisted UI
preference is applied to `i18n` *before* the pipeline is built, so a fresh
start summarizes in the language the user last selected — not the `[i18n]`
bootstrap default. Switching `general.language` in the TUI hot-rebuilds
the pipeline so the next analysis follows immediately. An explicit
`general.summary_language` entry always wins over the UI language.
