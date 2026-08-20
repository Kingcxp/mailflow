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

## Configuration

21. Writing the config back never materializes a resolved secret: a value that
    came from a `${VAR}` placeholder is written back as the placeholder, and an
    `api_key` resolved from `api_key_env` is written back empty.
22. `patch_config_value` is section-scoped: when the target section is absent
    it must report failure so the caller does a full rewrite, never patch a
    same-named leaf in a different section.
23. Every settings mutation re-validates the whole config and, on failure,
    raises `SettingsError` naming the offending option. Nothing is persisted
    from a config that does not validate.
24. For `[[llms]]` the list order is the routing policy: first entry is the
    default, each entry falls back to the ones after it. `default`/`fallback`
    are derived, and deleting an LLM scrubs every reference to it.

## Events

25. Runtime events are emitted under the `mailflow.` prefix
    (`mailflow.mail.processed`, `mailflow.action.reminder`, ...). Subscribers
    must use the emitted name; hosts and docs may not invent an unprefixed
    alias.

## Logging

26. Core never calls `basicConfig()` and never touches the root logger;
    `propagate=False` is scoped to `mailflow`.
27. Secrets are redacted from formatted messages, `exc_text` and exception
    args; persisted `ProcessorNote` text is sanitized too.

## i18n

28. `en` is the complete baseline; `zh-CN` keeps key parity (test-enforced).
29. External packs are data-only JSON; missing keys fall back to English.
30. `t()` interpolates only when parameters are passed, so a message
    containing literal braces (e.g. a `${ENV_VAR}` example) is returned
    verbatim.
31. The English pack contains English text: a translated string in `en.json`
    is a bug, not a shortcut.

## Docs

32. Behavioral changes update the relevant `docs/architecture` or
    `docs/agent` page in the same change. No `docs/ai` directory.
