# ADR 0002 — Pluggy for discovery, Core pipeline for execution

- Status: accepted
- Date: 2026-08-15

## Context

Plugins need a standard discovery/registration mechanism, but the processing
chain needs precise, testable semantics: priority ordering, per-processor
timeouts, retries and a failure policy. Using Pluggy's call-lists for
execution would entangle ordering with discovery and make policy implicit.

## Decision

- Pluggy is used only for discovery and registration: hooks
  `mailflow_plugin_info()` and `mailflow_register(registrar, config)`,
  entry-point group `mailflow.plugins`.
- `mailflow.pipeline.PipelineEngine` owns ordering (sorted bindings),
  retries, `asyncio.wait_for` timeouts, `continue`/`stop` failure policy,
  the `ProcessorNote` trail and the fallback-summary guarantee.
- Component ownership is recorded at registration time by
  `PluginRegistrar`/`ComponentRegistry`; runtime code never searches for
  "the first plugin with capability X".

## Consequences

- A plugin that raises during registration is isolated (logged, skipped);
  a processor that raises mid-pipeline is isolated by policy.
- Execution semantics are unit-testable without Pluggy (fake bindings) and
  covered end-to-end through the public API.
- Frozen builds register the official set via static imports in
  `mailflow-bundled`, independent of entry-point metadata.
