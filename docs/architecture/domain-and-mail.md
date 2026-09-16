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

The prompt's calibration is part of the contract: **`ad` is the smallest
bucket**. Bulk, automated or mass-mailed is explicitly *not* what makes a mail
`ad` — an institutional notice, newsletter with a date, recruitment or
internship invitation, workshop or library announcement, or anything the
recipient could act on is at least `info`. `ad` is reserved for mail that is
purely promotional, repetitive system chatter, or otherwise unusable. The
recipient profile (below) is authoritative for relevance, and the rolling
feedback notes apply to mail of the same kind only — they may not be used to
turn an announcement into `ad`.

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
and has `summary`, `action_type`, `due_at`/`due_end`, `notes`, optional
`location`/`url`, and an `origin`: `analysis`, `custom`, or `seminar`.
Pipeline-derived action items use `analysis`; user todos use `custom`; an
explicitly confirmed seminar uses `seminar`. The origin makes deletion safe:
analysis output is dismissed by its stable natural key so re-analysis keeps it
hidden, while custom and seminar entries are deleted from the custom-action
store for real.

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
malformed or unavailable batch is incomplete work, not an empty result.

`service.import_seminar()` is the only import path. It accepts user edits,
requires a title and future start time, validates timezone and end-after-start,
and writes one `ActionItem` with the stable candidate id. Repeated confirmation
returns that same item rather than creating a duplicate. Rejected proposals stay
hidden on later scans; expired proposals can only be imported after the user
corrects them to a future time.

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

## SmartActionIntent and SmartActionResult

`service.smart_action(instruction)` is the single natural-language entry point
behind the TUI's **Smart action** control. The model classifies the instruction
into a `SmartActionIntent`:

- `search` — the user wants mails found or filtered. `records` carry the ranked
  matches of `smart_search`.
- `schedule_seminar` — the user wants MailFlow to act on mails announcing an
  attendable event. The mails are matched first (so only mails the user's
  instruction actually concerns are used), then `discover_seminars` extracts
  the event and the proposals with a usable future start are written through
  `import_seminar`.

`SmartActionResult` reports the intent, the matched `records`, the same
`total_mails`/`failed_mails`/`failed_batches` completeness counters, the
`scheduled` entries it created, and `needs_review` — proposals it deliberately
did not guess about (no start time, or a start in the past). `needs_review`
reaches the user as an explicit review form; nothing is scheduled silently and
every scheduled entry is an ordinary, deletable `ActionItem` carrying its
source-mail backlink. Asking for the operation is the user's confirmation, so
the operation itself needs no second prompt; an unreadable intent answer means
`search` (filtering is always safe, writing never is).

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
