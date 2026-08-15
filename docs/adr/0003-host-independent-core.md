# ADR 0003 — Host-independent Core

- Status: accepted
- Date: 2026-08-15

## Context

MailFlow must run standalone (CLI), in a terminal UI (Textual), and later be
embedded into chat-bot frameworks (QQ-style) that already own their event
loops and logging. Those hosts have conflicting constraints; Core must not
couple to any of them.

## Decision

- `mailflow-core` contains all domain, pipeline and service logic and
  depends only on pydantic, pluggy, rich and tzdata.
- Hosts are thin clients: `mailflow-cli` (Typer), `mailflow-tui` (Textual)
  and future bot plugins render service data and call service methods; no
  business logic lives in hosts.
- `start_service(...)` is the single embedding entry point: it accepts
  injectable plugin managers, output streams and host log handlers, and
  never reconfigures the root logger (`propagate=False` scoped to
  `mailflow`).
- Concrete adapters live in `plugins/*`; `mailflow-bundled` is the
  composition seam that keeps them out of Core.

## Consequences

- A bot framework can start the full pipeline with one call, receive mail
  events over the async `EventBus`, manage state through `MailFlowService`,
  and reuse the shared `CommandRouter` for management commands.
- Core is testable without any UI; the E2E suite drives it through the
  public API only.
- Frozen builds include hosts explicitly; embedding hosts never pull in
  Typer/Textual transitively through Core.
