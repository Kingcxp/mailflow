# Invariants

These contracts are load-bearing. A change that violates one is a bug even
if all tests pass — update the test, not the contract.

## Urgency

1. `Urgency` has exactly four members with the public colors:
   `ad #909399`, `info #67C23A`, `important #E6A23C`, `urgent #F56C6C`.
2. Values and colors are serialized/stored as-is (e.g. `"urgent"`,
   `"#F56C6C"`); consumers may not rename them.

## Manual urgency

3. `manual_urgency` is an override layer: `effective_urgency = manual if
   set else auto`.
4. Setting manual urgency never modifies `auto_urgency`; reset
   (`manual_urgency = None`) restores the automatic value.

## Domain purity

5. `MailMessage` keeps the original `body_text`/`body_html`; analysis lives
   in `MailAnalysis`/`MailRecord`, never inside the message.
6. Snapshot types (`RuntimeSnapshot`, `AccountSnapshot`, ...) never import
   concrete adapters or UI frameworks.
7. Every `ActionItem` carries a `mail_id` backlink to its source mail.

## Plugins

8. Component ownership is stamped at registration time (`PluginRegistrar`).
   No capability-searching at runtime.
9. Component ids are the canonical adapter references in configuration
   (`fake`, `sqlite`, `openai-compatible`, `rules`, `llm-importance`,
   `console`).
10. Pluggy handles discovery/registration only; ordering, retries, timeouts
    and failure policy live in `mailflow.pipeline`.
11. One broken plugin must not kill startup; one failing source must not
    cancel other sources.

## Pipeline

12. A mail is never stored without a summary (fallback-summary guarantee).
13. `failure_policy = continue` really continues after retries are
    exhausted; `stop` halts the chain.

## Replies

14. `confirm_reply` requires a valid, unexpired token on a `prepared` draft.
15. The `sent` state is persisted **before** the provider send; a failed
    send reverts the draft without a token. No double send.
16. Confirmation calls only the source instance of the draft's account.

## Storage

17. Trash holds the full serialized record + deletion timestamp.
18. Purge compares the trash deletion timestamp, never the receipt time.
19. Restore returns the identical record (original `received_at`).
20. The first deletion timestamp is preserved across re-sync cleanup cycles.

## Logging

21. Core never calls `basicConfig()` and never touches the root logger;
    `propagate=False` is scoped to `mailflow`.
22. Secrets are redacted from formatted messages, `exc_text` and exception
    args; persisted `ProcessorNote` text is sanitized too.

## i18n

23. `en` is the complete baseline; `zh-CN` keeps key parity (test-enforced).
24. External packs are data-only JSON; missing keys fall back to English.

## Docs

25. Behavioral changes update the relevant `docs/architecture` or
    `docs/agent` page in the same change. No `docs/ai` directory.
