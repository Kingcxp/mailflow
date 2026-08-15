# ADR 0001 — uv workspace for all packages and plugins

- Status: accepted
- Date: 2026-08-15

## Context

MailFlow ships a host-agnostic core, several official plugins, hosts (CLI,
TUI) and a testkit. We need one lockfile, consistent quality-tool
configuration, and easy local development across all of them.

## Decision

A single uv workspace: root `pyproject.toml` with `members = packages/*
plugins/*`, a shared `[dependency-groups] dev`, and per-package
`tool.uv.sources` workspace mappings. One `uv.lock`; `uv sync
--all-packages --group dev` installs everything editable.

## Consequences

- Single source of truth for dev tooling (pytest/ruff/mypy/pyright/nuitka)
  and one lockfile; version drift between packages is impossible in dev.
- mypy resolves workspace packages as first-party via `mypy_path`; pyright
  via `extraPaths`.
- Independent distributions remain possible: each package has its own
  `pyproject.toml` and `uv build --all-packages` produces real wheels.
