# Setup

## Requirements

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/) ≥ 0.5
- git (for development)

## Install

```bash
uv sync --all-packages --group dev
```

This creates the workspace virtualenv with every package (editable) plus the
dev group (pytest, pytest-asyncio, coverage, ruff, mypy, pyright, nuitka).

## First run

```bash
uv run mailflow config-check -c configs/development.toml
uv run mailflow tui -c configs/development.toml        # add your accounts/LLMs first
uv run mailflow shell -c configs/development.toml
```

`configs/development.toml` ships with no accounts or LLMs — add your own
mailboxes and OpenAI-compatible endpoints there (both files contain commented
templates). The `mailflow-mail-fake` plugin remains available as a dev-only
source adapter if you want to experiment offline without a real mailbox.

For a real LLM, copy `configs/example.toml` to `configs/local.toml`, fill in
tokens (or export `MAILFLOW_LLM_GO_TOKEN`, `MAILFLOW_LLM_LOCAL_TOKEN`) and
start:

```bash
uv run mailflow run -c configs/local.toml
```

## Layout notes

- `data/` and `logs/` are created on first run and are gitignored.
- `configs/local.toml` is gitignored; never commit tokens.
- The workspace resolves packages as first-party source for mypy (via
  `mypy_path`) and pyright (via `extraPaths`) — see root `pyproject.toml`.

## Useful commands

```bash
uv run mailflow doctor -c configs/development.toml     # registration summary
uv run mailflow snapshot --json                        # machine-readable state
uv run mailflow command "mail list" -c configs/development.toml
```
