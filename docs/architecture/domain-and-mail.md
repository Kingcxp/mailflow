# Domain and mail model

All domain types live in `mailflow/domain.py` and are provider-neutral:
no concrete adapter, transport or UI type is imported.

## Urgency contract

`Urgency` has exactly four members with public colors:

| Member      | Value      | Color    | Meaning                                        |
| ----------- | ---------- | -------- | ---------------------------------------------- |
| `AD`        | `ad`       | #909399  | irrelevant advertising / junk                  |
| `INFO`      | `info`     | #67C23A  | useful, not time-critical (lecture notice)     |
| `IMPORTANT` | `important`| #E6A23C  | needs reading (verification code)              |
| `URGENT`    | `urgent`   | #F56C6C  | must be handled now or at a specific time      |

The product contract behind the table: **urgent** is reserved for concrete,
dated obligations aimed at the recipient — exams, compulsory meetings,
defenses, document pickups, payment/registration deadlines. Every
urgent/important mail with a stated time MUST yield an `ActionItem`, and
action items drive the reminder scheduler (early reminder the evening
before at `reminder_hour`, final reminder at `reminder_minute` before the
due time) — that is the "strict notification" path. Login notices,
password expirations and shipping notifications are `ad`-grade noise;
lectures without compulsory attendance are `info`. `important` means "read
and probably act today" without a hard timestamp. `manual_urgency` lets
the user correct any call; the correction is an override, never a
re-training signal.

`rank` orders them for notifier thresholds; `parse_urgency` normalizes
common LLM synonyms ("critical", "junk", "medium", case variants) to the
canonical values.

## MailMessage

The normalized, provider-independent mail: identity (`message_id`,
`account_id`), envelope (`sender`, `recipients`, `cc`), `subject`,
`date`/`received_at` (timezone-aware), and the **original** `body_text` /
`body_html`. Analysis is never stored inside the message.

`normalized_message_id()` returns the provider id when present, else the
RFC message id (kept by forwarders), else a content digest over
sender/subject/date/body — it is the record identity in storage and is
**account-independent**, so forwarded copies of the same mail arriving
through several accounts deduplicate to one record (the runtime skips
already-known ids before processing/notifying).

`MailMessage.image_text` carries text recognised from the mail's own images
(an event poster usually holds the title, date and room the body omits). It is
empty unless `general.ocr_images` is on and the optional OCR package is
installed; it is kept apart from the bodies because it is derived data, and
`_plain_body` appends it (labelled) so every prompt and every literal search
sees it without further wiring. See `docs/architecture/pipeline.md`.

## Mail sources and the optional history capability

`MailSource` (in `mailflow/contracts.py`) owns the live stream: `run(emit,
stop_event)`, `send_reply` and `close`. A source **may** additionally
implement `HistoryCapableSource`:

```python
async def fetch_history(self, limit: int = 50, offset: int = 0) -> list[MailMessage]: ...
```

Both are runtime-checkable protocols, so the capability is discovered with
`isinstance` and existing sources stay valid without changes.

`fetch_history` returns already-received mail newest-first and **never emits**:
the caller decides what to do with it. The service exposes
`history_accounts()`, `fetch_history(account_id, limit=, offset=)`,
`is_mail_known(mail)` and `process_mail(mail)`; the last one routes through the
runtime, so a user-picked historical mail follows exactly the same path as a
streamed one — dedup by normalized id, persistence retry,
`mailflow.mail.processed`, notifier thresholds — and returns `None` when the
mail was already stored.

Browsing must not disturb the live stream: the built-in IMAP source pages over
UIDs for history without touching the incremental poll water-mark.

If an IMAP MIME payload unexpectedly defeats the standard parser, the source
emits a deterministic fallback `MailMessage` rather than blocking that UID and
every newer message. The fallback carries `parse_error` (the exception type,
never raw mail data); the pipeline persists it with a visible failed
`source-parser` note and its normal fallback summary. Malformed transport input
therefore cannot disappear silently.

## MailAnalysis

The structured interpretation produced by the processor chain: `summary`,
`urgency`, `reason`, `reply_required`, `suggested_reply`, `action_items`,
`notes`, and `backend` (the LLM backend plugin actually used, if any).

The prompt's calibration is part of the contract, and it judges by **what the
recipient must do**: anything to act on, answer or track makes the mail at least
`important` (a stated deadline or a required action does so even inside a bulk
notice, newsletter or invitation), `info` is for genuinely optional/FYI mail,
and **`ad` is reserved for unusable mail**. Bulk, automated or mass-mailed is explicitly *not* what makes a mail
`ad` — an institutional notice, newsletter with a date, recruitment or
internship invitation, workshop or library announcement, or anything the
recipient could act on is at least `info`. `ad` is reserved for mail that is
purely promotional, repetitive system chatter, or otherwise unusable. The
recipient profile (below) is authoritative for relevance, and the rolling
feedback notes apply to mail of the same kind only — they may not be used to
turn an announcement into `ad`.

**Action items are only for what the recipient must do and cannot skip.** The
prompt creates an `ActionItem` only for a deadline the recipient is liable for
(a fee, a required submission, an exam), an appointment they must attend, a
pickup, or a meeting/conference they are required to attend or must reply to.
Optional or self-selected matters yield no action item however concrete their
date is: they remain in `summary` and `reason`. A registration is actionable
only when failing to do it has a consequence for the recipient's standing.
Every emitted item must explicitly include `must_do: true`; the payload field
defaults to false and Core drops missing, false, or malformed flags. This is an
enforced gate, not just prompt guidance. One obligation yields one item. The
`action_type` is any non-empty label; `exam`, `meeting`, `errand`, and `other`
are built-in labels, not an exhaustive vocabulary. Urgency is unaffected by
this rule. Seminars that should enter the schedule use the explicit import path
or the smart action's `schedule_seminar` tool.

**Dates resolve against the mail, not against now.** The per-mail context
carries the mail's own send time (`MailMessage.date`, rendered in both UTC and
the configured local zone) and its age in days, because the body's relative
wording — "tomorrow", "next Friday", "明天", "下周一" — refers to the day the
mail was written, not to analysis time. Re-fetching an older mailbox therefore
cannot turn a September "tomorrow" into a deadline today. As a second line of
defence the processor drops any item whose parsed `due_at` is already in the
past, so a historical mail's expired obligation never becomes a stored schedule
entry (an explicitly stated *future* deadline in an old mail is still kept).
The reminder scheduler independently selects only items with
`day_start <= due_at`, and `purge_expired_actions` retires spent entries.

When no processor supplies a non-empty summary, the pipeline stores the source
subject only as a display fallback and marks `summary_is_fallback=True`.
`MailRecord` keeps recognizing the corresponding legacy pipeline note for
records written before that field existed. A host shows that stored content —
the mail's real summary or, for a fallback, the source subject **labelled** as
not generated — plus a localized failed/partial status naming the processor
cause when a note failed. A real non-empty urgency reason remains visible; an
absent reason is explicitly unavailable. The original mail body remains
visible. A failed processor note always carries a cause: an exception whose
message is empty is recorded by its type name, so a persisted note can never
read `failed: ` with nothing after it.

## ActionItem

`ActionItem` is a provider-neutral timed entry in the reminder schedule. Every
item retains its `mail_id` source field (empty only for a user-created todo),
and has `summary`, a non-empty `action_type`, `due_at`/optional `due_end`,
`notes`, optional `location`/`url`, and an `origin`: `analysis`, `custom`, or
`seminar`. Analysis creates items only when the model explicitly marks an
obligation `must_do: true`; user todos use `custom`; confirmed seminars use
`seminar`. The origin makes mutation safe: analysis output is dismissed by its
stable natural key so re-analysis keeps it hidden, while custom and seminar
entries are edited or removed from the custom-action store.

`service.purge_expired_actions()` retires spent entries on a periodic sweep
(hourly, plus once at startup). "Spent" means the entry ended more than a day
ago — measured from `due_end` when the entry has a window, so a meeting that is
still running keeps its reminder. Mail-derived entries are dismissed by the same
natural key a manual delete records, so re-analysis cannot resurrect them;
user todos and imported seminars are removed from the custom-action store. The
delay is deliberate: the entry the user is looking at, and the one whose
reminder just fired, must survive long enough to be acted on.
`service.is_action_expired()` exposes the same "already over" judgement for the
TUI, which dims such a row instead of hiding it.

**One overlapping event, one visible entry.** `actions_duplicate()` normalizes
only the summary (Unicode case-folding plus collapsed whitespace); action type,
mail id, and seminar title markers do not affect identity. Time windows are
half-open `[start, end)`: equal point events match; a point matches a window
only inside its bounds; two windows match only when their overlap has positive
duration. Touching endpoints are not duplicates. A malformed/non-positive
stored window is treated as a point. `dedupe_actions()` keeps the earliest
start, preserving input order for ties. `list_actions()` and the reminder
scheduler use this same rule; `list_actions_all()` retains all stored rows for
id-based mutation. `add_action()` and `edit_action()` reject a conflicting item
with the same predicate rather than creating a second visible event. Analysis
results are deduped against existing mail-derived and custom actions when saved.

## SeminarCandidate and SeminarDiscoveryResult

`SeminarCandidate` is **not** an `ActionItem`: it is a review proposal grounded
in one `mail_id`, with title, optional start/end timestamps and IANA timezone,
location/URL/description, confidence, and a short source evidence excerpt. A
candidate begins `pending`, can be `rejected`, becomes `expired` when its stated
start has passed, and becomes `imported` only after confirmation.

`service.discover_seminars()` scans stored mail in bounded LLM batches. It gives
the model per-batch opaque aliases rather than raw record ids, validates the
model JSON, and persists review proposals; it never creates reminders directly.
`SeminarDiscoveryResult` reports total, successfully evaluated, and failed mail
counts plus failed batches, so a host can expose partial work honestly. A
malformed or unavailable batch is incomplete work, not an empty result. Each
mail travels with its own `sent_at` (the mail's `Date` header), because a notice
is routinely fetched weeks after it was written and a year-less date in its body
("15 September") can only be resolved against the mail's own send time — never
against the current time, which used to make such notices look undated. The
reply budget is sized for a whole batch of event objects, and a reply cut short
by it is salvaged object-by-object: the complete candidates are kept while the
batch is still reported as incomplete, so a token limit cannot silently discard
an entire batch's findings.

`service.import_seminar()` is the only import path. It accepts user edits,
requires a title and future start time, validates timezone and end-after-start,
and writes one `ActionItem` with the stable candidate id, whose title always
carries the `[SEMINAR] ` marker (added idempotently, so a re-import or a caller
that already typed it never stacks prefixes). Repeated confirmation returns that
same item rather than creating a duplicate. Rejected proposals stay hidden on
later scans; expired proposals can only be imported after the user corrects them
to a future time.

The **Smart action** tool loop reaches this same path through the
`schedule_seminar` / `schedule_seminars` tools, so a seminar the model found is
staged, confirmed and imported exactly like one the user reviewed by hand.
`schedule_seminars` takes a whole list of candidate ids in one call: the loop
has a bounded number of steps, so staging one candidate per call would cap how
many seminars could ever reach the schedule however many the scan found.

`list_seminar_candidates()` collapses candidates that describe the same event
(the same normalized title at the same start instant, or two untimed notices
with the same title) — a seminar and its later reminder are separate mails, so
without this the same talk appears once per announcement. The survivor is the
most confident description, unless a duplicate is already imported or rejected,
in which case that state wins; the folded-away candidate ids remain importable
through `import_seminar`.

## MailRecord

The stored unit: `mail` + `analysis` + `auto_urgency` + `manual_urgency` +
`processor_notes` + `received_at`.

- `effective_urgency` = `manual_urgency` if set, else `auto_urgency`.
  Manual is an override **layer** — `auto_urgency` is never overwritten, so
  reset (`manual_urgency = None`) restores the automatic result.
- `summary` falls back to the subject when no analysis exists.
- `action_items` are the analysis action items (empty without analysis).

The pipeline guarantees that after processing a record always has a summary.
Its subject-based fallback is recorded as a pipeline note and explicitly marked
on `MailAnalysis`, so consumers can distinguish storage continuity from a
successful content analysis.

## SmartSearchResult

`SmartSearchResult` is the provider-neutral result of an intent search. Its
`records` are ordered by model-provided relevance (then receipt time), not by
message id. Scored candidates below 40 are model-declared non-matches and are
not returned or streamed into the TUI; legacy unscored candidate arrays remain
accepted for compatibility. `total_mails`, `failed_mails`, and `failed_batches`
expose whether the result is complete: a caller must show an explicit incomplete
state rather than treating unavailable or malformed batches as no matches.
Candidate aliases exist only inside one LLM batch; raw record ids are never
exposed to the model.

## Smart action: a tool-calling loop

`service.smart_action(instruction)` is the single natural-language entry point
behind the TUI's **Smart action** control. It does not classify the instruction
into a fixed set of intents: it hands the model the request plus a set of tools
and runs a bounded loop (`mailflow/tools.py`, `run_agent`). The model decides
which tool to call, with which arguments, in which order; the user states a
goal, never a procedure.

The tools (`ToolRegistry.specs()` returns them as provider-independent
JSON-schema definitions):

| Tool | Kind | What it does |
| ---- | ---- | ------------ |
| `find_mail` | read | `contains` is a literal, case-insensitive substring over subject, body/HTML, attachment file names, and recognized image text; `query` is semantic ranking. A literal condition decides membership; the optional semantic pass only orders those matches. The response shows up to `limit` rows (capped at 40), reports the total match count, and returns every matching mail in `SmartActionResult.records`. A `search_id` is issued only for a complete non-empty result and covers every match, not just the preview. |
| `delete_mail` | staged | Available only when the original user instruction explicitly requests deletion; search/filter/cleanup language alone is not authorization. Give exactly one of `record_ids` or a complete `find_mail` `search_id`. The one-use token stages every match, including rows beyond the preview; incomplete searches issue no token. |
| `schedule_event` | staged | Validates an absolute future ISO-8601 start, optional end-after-start, a non-empty built-in or custom type, notes, location, and URL; stages a plain schedule entry. |
| `schedule_seminar` / `schedule_seminars` | staged | Stages the seminar import path for `check_seminars` candidates, so the `[SEMINAR]` marker is applied by the service rather than the model. A candidate whose mail states no time becomes a review item instead. |
| `list_actions` | read | The visible deduplicated schedule with item ids. |
| `add_action` / `edit_action` / `delete_action` | staged | Validate a user-owned entry and stage the change. Mail-analysis entries cannot be edited because their source owns the content; deleting one records the stable dismissal key. |
| `check_seminars` | read | `discover_seminars` over an optional mail subset; reports only candidates that have not started, plus their candidate ids. |

**Staged means nothing happened.** A mutating tool returns a
`PendingOperation` (`tool`, `arguments`, `record_ids`, `summary_key`,
`summary_params`) and touches no storage. The TUI presents schedule and seminar
operations in an editable review modal, with Apply and Cancel paths; mail
deletion gets a separate confirmation. Only after confirmation does the host
apply the reviewed operations through the service (`delete_mails`,
`import_seminar`, `add_action`, `edit_action`, `delete_action`). A model's
judgement is never enough to delete mail or edit the schedule, and mail
deletion stays recoverable (trash).

Loop mechanics: at most `_MAX_TOOL_STEPS` (12) model↔tool round trips; each tool
result is fed back as a `role: "tool"` message keyed by `tool_call_id`; an
unknown tool, a rejected call or a tool exception becomes an `error: …` text
for the model instead of aborting the operation. `progress(stage, done, total,
detail)` reports `llm`, `tool`, `tool_done`, and `finalizing` phases; tool
arguments and results are bounded snippets. The TUI records these phases in a
progress log, and the whole loop is cancellable.

`SmartActionResult` reports `records` (all matches returned by `find_mail`),
`pending` (the staged operations), `tool_steps` (what actually ran, e.g.
`find_mail {"contains": "seminar"}`), `final_text` (the model's closing
summary), and the `total_mails`/`failed_mails`/`failed_batches`/
`failure_reasons` completeness counters.


## Recipient profile

`feedback.profile` holds the recipient's own description of who they are and
which mail matters to them (`service.user_profile()` / `set_user_profile()`,
trimmed and capped at 4000 characters; an empty save clears it). It is threaded
into `ProcessingContext.user_profile` for every analysis and into the smart
action/search requests, so both the classification and the matching follow that
person's situation. Because it is user-authored **data**, it is sent in the
user message — a mail can never impersonate it as an instruction.

## Expired mail

`service.list_expired_mails()` is the only definition of "expired", and
`purge_expired_mails()` moves exactly those records to the trash (returning the
count), so the one-click clear is the same recoverable delete the per-mail
button performs. A record is expired when **all** of these hold:

- `manual_urgency is None` — a manual classification is never overridden by an
  automatic decision;
- the analysis **completed**: no missing analysis, no fallback summary and no
  failed processor note. Nothing was extracted from such a mail, so nothing can
  be known to have passed — it is evidence for keeping, never for deleting;
- `effective_urgency` is `ad` or `info` — urgent/important mail stays, because
  passing dates do not make exam material, receipts or decisions stop mattering;
- nothing is scheduled ahead: no `action_items` entry (in the record **or** in a
  custom/seminar entry that keeps this mail's `mail_id`) has `due_end or due_at`
  in the future. An event whose window is still running is not over;
- for `ad`, the mail is at least 24 hours old, so the message being read right
  now is never swept away; for `info`, it must have announced at least one timed
  item that has already passed (undated FYI mail has no expiry signal and stays).

The purge deletes exactly the ids a confirmation dialog listed, re-reading and
re-checking each record immediately before its own delete: a manual
classification, a re-analysis or a new schedule entry landing while the dialog
is open wins over the earlier snapshot. The count it reports is what this call
really moved, and the retention window is refreshed, so a mail that was already
sitting in the trash cannot expire minutes after the user was told it is
restorable.

## TrashRecord

A recoverable copy of the full record plus `deleted_at` (the deletion
timestamp — purge compares against this, never the receipt time) and
`expires_at`. `to_mail_record()` restores the identical record.

## ReplyDraft

Editable reply with a state machine (`draft` → `prepared` → `sent` |
`cancelled`) and a short-lived confirmation token. See `replies.md`.

## Runtime snapshots

`PluginSnapshot`, `ComponentSnapshot`, `AccountSnapshot`, `LLMSnapshot` and
`ProcessorBindingSnapshot` describe **registrations** (plugin ids, component
ids, account status, LLM→backend mapping, processor→LLM/fallback bindings),
never concrete adapter objects — any host can render them without importing
plugins.

## Command responses

`CommandResponse` carries `spans` of `(text, style)` — Rich styling as
metadata. Plain text is derived for transports without rich support; Core
never embeds ANSI bytes in strings.
