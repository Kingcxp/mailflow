# Agent documentation

This directory is for **AI agents** (and humans) modifying MailFlow. It is
intentionally named `agent`, not `ai`.

- `invariants.md` — contracts that must never be violated
- `module-map.md` — which file owns which concern
- `change-playbook.md` — safe procedures for common change types

Start with `invariants.md`, then `module-map.md`. The root `AGENTS.md`
summarizes the rules; `MAILFLOW_FROM_ZERO.md` is the staged reconstruction
plan with per-stage commits; `docs/build-log/BUILD_LOG.md` records what was
actually executed.
