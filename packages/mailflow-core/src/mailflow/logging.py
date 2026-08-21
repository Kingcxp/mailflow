"""Queue-based logging with rich console output, rotating file, optional JSONL
and secret redaction.

Design constraints:

- Core never calls ``basicConfig()`` and never touches the root logger.
- ``propagate=False`` is scoped to the ``mailflow`` logger; a host embedding
  MailFlow keeps full control of its own logging configuration.
- All records flow through a :class:`logging.handlers.QueueHandler` into a
  background :class:`logging.handlers.QueueListener`, so plugin/source code
  never blocks on slow sinks.
- Host applications may inject their own handlers via ``extra_handlers``.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import queue as queue_module
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TextIO

from rich.console import Console
from rich.logging import RichHandler

from mailflow.config import LoggingConfig

logger = logging.getLogger("mailflow.logging")

MAILFLOW_LOGGER = "mailflow"
_MAX_QUEUE = 2000


class SecretRedactionFilter(logging.Filter):
    """Redacts configured secrets from messages, formatted text and tracebacks."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = [secret for secret in secrets if secret]

    def add_secret(self, secret: str) -> None:
        if secret and secret not in self._secrets:
            self._secrets.append(secret)

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = self._redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        if record.exc_text:
            record.exc_text = self._redact(record.exc_text)
        if record.exc_info is not None:
            # Formatters may re-format from exc_info; neutralise the payloads.
            _, exc_value, _ = record.exc_info
            if isinstance(exc_value, BaseException):
                exc_value.args = tuple(
                    self._redact(a) if isinstance(a, str) else a for a in exc_value.args
                )
        return True


class JsonlHandler(logging.Handler):
    """One JSON object per line: time, level, logger and message."""

    def __init__(self, stream: TextIO) -> None:
        super().__init__()
        self._stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = {
                "time": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            self._stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)


class LoggingRuntime:
    """Owns the queue listener and handlers created by ``configure_logging``."""

    def __init__(
        self,
        mailflow_logger: logging.Logger,
        queue_handler: logging.handlers.QueueHandler,
        listener: logging.handlers.QueueListener,
        sinks: Sequence[logging.Handler],
        extra_handlers: Sequence[logging.Handler],
        opened_files: Sequence[TextIO],
    ) -> None:
        self._logger = mailflow_logger
        self._queue_handler = queue_handler
        self._listener = listener
        self._sinks = list(sinks)
        self._extra_handlers = list(extra_handlers)
        self._opened_files = list(opened_files)

    @property
    def redaction_filter(self) -> SecretRedactionFilter | None:
        for filt in self._queue_handler.filters:
            if isinstance(filt, SecretRedactionFilter):
                return filt
        return None

    def add_secret(self, secret: str) -> None:
        redactor = self.redaction_filter
        if redactor is not None:
            redactor.add_secret(secret)

    def close(self) -> None:
        """Remove handlers from the mailflow logger and stop the listener."""
        from contextlib import suppress

        self._logger.removeHandler(self._queue_handler)
        redactor = self.redaction_filter
        if redactor is not None:
            with suppress(Exception):
                self._logger.removeFilter(redactor)
        for handler in self._extra_handlers:
            self._logger.removeHandler(handler)
        with suppress(Exception):
            self._listener.stop()  # drains remaining queued records
        for handler in [*self._sinks, *self._extra_handlers]:
            with suppress(Exception):
                handler.close()
        for stream in self._opened_files:
            with suppress(Exception):
                stream.close()
        logger.debug("mailflow logging runtime closed")


_active_runtime: LoggingRuntime | None = None


def get_active_runtime() -> LoggingRuntime | None:
    return _active_runtime


def configure_logging(
    config: LoggingConfig,
    *,
    secrets: Iterable[str] = (),
    extra_handlers: Sequence[logging.Handler] | None = None,
    console_stream: TextIO | None = None,
    force_reconfigure: bool = True,
) -> LoggingRuntime:
    """Configure the ``mailflow`` logger tree; returns a closable runtime.

    ``extra_handlers`` are attached directly to the ``mailflow`` logger (host
    injection, e.g. a TUI log feed). When ``force_reconfigure`` is true any
    previously configured runtime is closed first.
    """
    global _active_runtime
    if force_reconfigure and _active_runtime is not None:
        _active_runtime.close()
        _active_runtime = None

    mailflow_logger = logging.getLogger(MAILFLOW_LOGGER)
    mailflow_logger.setLevel(config.level.upper())
    mailflow_logger.propagate = False
    if not mailflow_logger.handlers:
        mailflow_logger.addHandler(logging.NullHandler())

    for name, level in config.logger_levels.items():
        child = logging.getLogger(name)
        child.setLevel(level.upper())
        child.propagate = True  # keep routing through the mailflow queue

    redactor = SecretRedactionFilter(secrets)
    queue: queue_module.Queue[logging.LogRecord] = queue_module.Queue(maxsize=_MAX_QUEUE)
    queue_handler = logging.handlers.QueueHandler(queue)
    queue_handler.addFilter(redactor)
    queue_handler.setLevel(config.level.upper())

    sinks: list[logging.Handler] = []
    opened_files: list[TextIO] = []

    if config.console:
        console_target: TextIO = console_stream or sys.stdout
        if config.console_redirect:
            redirect_stream = open(config.console_redirect, "a", encoding="utf-8")  # noqa: SIM115 — lifetime stream owned by runtime
            opened_files.append(redirect_stream)
            console_target = redirect_stream
        rich_handler = RichHandler(
            console=Console(file=console_target, force_terminal=console_stream is None),
            show_time=True,
            show_path=False,
            markup=False,  # log text is data, not markup — brackets must render literally
            rich_tracebacks=False,  # tracebacks re-rendered from redacted exc_text
            level=config.console_level.upper(),
        )
        rich_handler.setLevel(config.console_level.upper())
        sinks.append(rich_handler)

    if config.file:
        path = config.file_path
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=config.file_max_bytes,
                backupCount=config.file_backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(config.file_level.upper())
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            sinks.append(file_handler)

    if config.jsonl:
        Path(config.jsonl_path).parent.mkdir(parents=True, exist_ok=True)
        jsonl_stream = open(config.jsonl_path, "a", encoding="utf-8")  # noqa: SIM115 — lifetime stream owned by runtime
        opened_files.append(jsonl_stream)
        jsonl_handler = JsonlHandler(jsonl_stream)
        jsonl_handler.setLevel(config.jsonl_level.upper())
        sinks.append(jsonl_handler)

    extra = list(extra_handlers or [])
    listener = logging.handlers.QueueListener(queue, *sinks, respect_handler_level=True)
    listener.start()

    mailflow_logger.addHandler(queue_handler)
    # the redactor mutates records in place; filtering at the logger (not only
    # the queue handler) keeps host-injected handlers behind it regardless of
    # handler registration order
    mailflow_logger.addFilter(redactor)
    for handler in extra:
        mailflow_logger.addHandler(handler)

    runtime = LoggingRuntime(
        mailflow_logger=mailflow_logger,
        queue_handler=queue_handler,
        listener=listener,
        sinks=sinks,
        extra_handlers=extra,
        opened_files=opened_files,
    )
    _active_runtime = runtime
    return runtime


def get_logger(name: str) -> logging.Logger:
    """Logger under the mailflow hierarchy, e.g. ``mailflow.runtime``."""
    if not name.startswith(MAILFLOW_LOGGER + "."):
        name = f"{MAILFLOW_LOGGER}.{name}"
    return logging.getLogger(name)


__all__ = [
    "MAILFLOW_LOGGER",
    "JsonlHandler",
    "LoggingRuntime",
    "SecretRedactionFilter",
    "configure_logging",
    "get_active_runtime",
    "get_logger",
]
