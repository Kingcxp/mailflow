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
  empty failure reason. **A timeout is terminal for that processor** — it is
  not retried, because a second attempt on the same slow call only doubles the
  wait the user configured while the UI still says "analysing" long after the
  deadline. The per-request budget is `[[llms]] timeout_seconds` (also 120 s by
  default) — raise both for a slow endpoint.
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


## Reading text out of images (optional)

Many event notices put the title, date and room only in an attached poster. The
body then carries the greeting, so the analyser sees no date and a search for
the event's own name finds nothing.

`mailflow/ocr.py` reads that text. It is **opt-in twice** — `general.ocr_images
= true` *and* the optional `rapidocr-onnxruntime` package — and every entry
point degrades to "no text" rather than raising, so a mailbox without the
package behaves exactly as before. The engine ships its own ONNX models, which
is why it is preferred over `pytesseract` (that one needs a separately
installed Tesseract binary).

The work happens inside `parse_mime` in the mail source, on the parsing
thread, because that is the only point where the image bytes exist: the storage
backend strips attachment payloads by design. The result travels as
`MailMessage.image_text`, kept apart from `body_text`/`body_html` because those
are the original contents and this is derived, optional data.

Only images that look like posters are attempted (`ocr.should_attempt`): images
within a size budget, with a name, and without chrome tokens such as `logo`,
`qr` or `signature`. `banner` and `header` are deliberately *not* in that list
— a real observed poster was named `..._leadership_talk_series_banner_2000x1050
_....jpg`, so skipping those names would drop exactly the mails this feature
exists for.

Recognition splits words at glyph gaps ("PAIR Research Se minar" is a real
result for "… Seminar"), so `ocr.searchable_forms` returns both the text
as-read and a gap-closed variant. A literal search matches either one: the
as-read form keeps ordinary phrases working, and the closed form finds a word
the engine broke in two. The closed form alone is never used — it also glues
legitimate word boundaries ("Room Y908 and Zoom" becomes "roomy908 andzoom").
