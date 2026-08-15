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

`rank` orders them for notifier thresholds; `parse_urgency` normalizes
common LLM synonyms ("critical", "junk", "medium", case variants) to the
canonical values.

## MailMessage

The normalized, provider-independent mail: identity (`message_id`,
`account_id`), envelope (`sender`, `recipients`, `cc`), `subject`,
`date`/`received_at` (timezone-aware), and the **original** `body_text` /
`body_html`. Analysis is never stored inside the message.

`normalized_message_id()` returns the provider id when present, else a
content digest — it is the record identity in storage.

## MailAnalysis

The structured interpretation produced by the processor chain: `summary`,
`urgency`, `reason`, `reply_required`, `suggested_reply`, `action_items`,
`notes`, and `backend` (the LLM backend plugin actually used, if any).

## ActionItem

A timed obligation extracted from a mail, with a `mail_id` backlink so the
action table can drill down into the source mail. Fields: `summary` (what),
`action_type` (`exam` | `meeting` | `errand` | `other`), `due_at`/`due_end`
(the time window), `notes` (what to bring, what to wear, materials).

## MailRecord

The stored unit: `mail` + `analysis` + `auto_urgency` + `manual_urgency` +
`processor_notes` + `received_at`.

- `effective_urgency` = `manual_urgency` if set, else `auto_urgency`.
  Manual is an override **layer** — `auto_urgency` is never overwritten, so
  reset (`manual_urgency = None`) restores the automatic result.
- `summary` falls back to the subject when no analysis exists.
- `action_items` are the analysis action items (empty without analysis).

The pipeline guarantees that after processing a record always has a summary
(subject-based fallback recorded as a pipeline note).

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
