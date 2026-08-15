# Logging

All logging flows through `mailflow/logging.py`. Goals: rich terminal output,
durable and re-routable logs, and full isolation from a host application's
logging configuration.

## Design

- A `mailflow` logger tree; `propagate=False` is scoped to that logger.
  Core **never** calls `basicConfig()` and never touches the root logger.
- Records pass through a `QueueHandler` into a background `QueueListener`
  (`respect_handler_level=True`), so sources/processors never block on slow
  sinks.
- Sinks, all configurable in `[logging]`:
  - Rich console handler (stdout; optionally redirected to a file via
    `console_redirect` — "terminal output elsewhere").
  - Rotating text file (`file_path`, `file_max_bytes`, `file_backup_count`).
  - Optional JSONL file (one JSON object per line).
  - Injectable host handlers (`extra_log_handlers`, e.g. the TUI log feed).
- Per-logger levels via `logger_levels`; per-sink levels.

## Secret redaction

`SecretRedactionFilter` redacts configured secrets (LLM API keys) from
formatted messages, `exc_text` and exception args — before any sink renders
them. `rich_tracebacks` is disabled so tracebacks are rendered from the
redacted text only. The runtime can add secrets after configuration via
`LoggingRuntime.add_secret`.

Transport plugins additionally sanitize their own error text (no URLs/query
strings), and the pipeline sanitizes persisted `ProcessorNote` text — logs
are not the only place secrets could leak.

## Configuration

`configure_logging(config, secrets=..., extra_handlers=..., console_stream=...)`
returns a `LoggingRuntime`; calling it again closes the previous runtime
(handlers removed, listener stopped with draining). `start_service` owns one
runtime for the service lifetime and closes it on startup failure too.

## Tests

Unit tests assert: the root handler list is unchanged before/after
configuration; `propagate=False` is scoped to `mailflow`; bearer/token text
is redacted in messages and tracebacks; double configuration leaves exactly
one queue chain.
