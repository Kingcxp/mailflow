# Agent documentation

This directory is for **AI agents** (and humans) modifying MailFlow. It is
intentionally named `agent`, not `ai`.

- `invariants.md` — contracts that must never be violated
- `module-map.md` — which file owns which concern
- `change-playbook.md` — safe procedures for common change types

Start with `invariants.md`, then `module-map.md`. The root `AGENTS.md`
summarizes the rules; `docs/build-log/BUILD_LOG.md` records what was
actually executed.

## Docs are checked, not trusted

`make docs` (`tools/check_docs.py`) fails when a document names a path,
`make` target, event, service method or plugin id that does not exist in the
code — see `../development/quality.md` for the exact checks. So when you
rename or delete something, the gate tells you which docs to update; it
cannot tell you whether the *prose* is still true, which is what invariant 34
is for.

`CHANGELOG.md` and the build log are exempt: they record history, including
things that were later removed.
