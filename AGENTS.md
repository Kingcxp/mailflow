# Agent rules for modifying MailFlow

Rules for AI agents (and humans) making changes to this repository. Read
`docs/agent/` for invariants, the module map and the change playbook; the
staged reconstruction plan lives in `MAILFLOW_FROM_ZERO.md`.

## First-entry rules

1. Read `docs/agent/invariants.md` before touching code. It enumerates the
   contracts that must never be violated (urgency colors, manual-urgency
   semantics, reply confirmation gate, logging isolation, plugin ownership).
2. Read `docs/agent/module-map.md` to find which package/file owns a concern.
   Follow existing patterns; a second convention beside an existing one is
   prohibited.
3. For a cross-file rename or refactor use the language server
   (`lsp` rename/references) — text-only renames silently drop callsites.
4. Never commit real mailbox credentials or LLM tokens. Secrets enter via
   environment-variable placeholders (`${VAR}`) or `api_key_env`.

## Core rules

5. `mailflow-core` is host-agnostic: it must never import concrete plugins,
   `typer`, `textual` or `httpx`. Concrete adapters live in `plugins/*`.
6. Component ownership is assigned at registration time (`PluginRegistrar`).
   Never add "find the first plugin with capability X" logic.
7. Pluggy is for discovery/registration only. Processor ordering, retries and
   failure policies belong to `mailflow.pipeline`.
8. `manual_urgency` is an override layer. Never overwrite `auto_urgency`
   with the manual value; reset restores the automatic result.
9. A reply can only be sent through `confirm_reply` with a valid, unexpired
   token. The SENT state is persisted before the provider send so a crash
   cannot double-send; a failed send reverts the draft without a token.
10. Core never calls `basicConfig()` and never reconfigures the root logger.
    `propagate=False` is scoped to the `mailflow` logger.
11. Logs must never contain secrets. Add secrets to the redaction filter at
    configuration time; error text from transports must be sanitized before
    it can reach a persisted `ProcessorNote`.
12. All UI text is data-driven through `service.t(key)`; never hardcode user
    visible strings. zh-CN must keep key parity with en (enforced by tests).
13. Docs duty: any behavioral change updates the relevant `docs/architecture`
    or `docs/agent` page in the same change. Do not use the `docs/ai` name —
    agent docs live in `docs/agent/`.
14. Verify before committing: run the specific tests covering the change and
    `make check` (lint, format, mypy, pyright, pytest, docs gate).

## Build log

Every stage of the reconstruction is recorded in
`docs/build-log/BUILD_LOG.md` with the commands actually executed. Do not
claim a check passed unless it was run.
