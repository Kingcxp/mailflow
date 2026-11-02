"""Localized labels and credential-safe error presentation for the TUI."""

from __future__ import annotations

import logging
import re
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from typing import Any, cast

from mailflow.config import is_secret_key
from mailflow.domain import MailRecord, Urgency

_URGENCY_LABEL_KEYS: dict[str, str] = {
    "ad": "tui.urgency_opt_ad",
    "info": "tui.urgency_opt_info",
    "important": "tui.urgency_opt_important",
    "urgent": "tui.urgency_opt_urgent",
}


def urgency_label(service: Any, urgency: Urgency | str) -> str:
    """Render a localized label without changing the serialized enum value."""
    value = urgency.value if isinstance(urgency, Urgency) else str(urgency)
    key = _URGENCY_LABEL_KEYS.get(value)
    return service.t(key) if key else value


def relabel_buttons(owner: Any, labels: Mapping[str, str]) -> None:
    from textual.widgets import Button

    for ident, key in labels.items():
        button = owner.query_one_optional(f"#{ident}", Button)
        if button is not None:
            button.label = owner._service.t(key)


def analysis_status(service: Any, record: MailRecord) -> str:
    """Use the same failure/fallback status in mail and action details."""
    failed = [note for note in record.processor_notes if note.status == "failed"]
    if failed:
        key = (
            "tui.detail_analysis_failed"
            if record.analysis_is_fallback
            else "tui.detail_analysis_partial"
        )
        details: list[str] = []
        for note in failed[:2]:
            # Older stored notes contain only text. Retain an HTTP status number
            # without exposing backend prose, URLs or credentials.
            status = re.search(r"\bHTTP\s+([1-5]\d{2})\b", note.message, re.IGNORECASE)
            detail = (
                service.t("errors.http", status=status.group(1))
                if status
                else service.t("errors.operation")
            )
            details.append(f"{note.processor_id}: {detail}")
        error = "; ".join(details)
        return str(service.t(key, error=error_detail(service, error)))
    return str(service.t("tui.detail_analysis_unavailable")) if record.analysis_is_fallback else ""


def _is_secret_field(key: str) -> bool:
    """Whether a configuration field's values must never reach the UI."""
    normalized = key.replace("-", "_").lower()
    return not normalized.endswith("_env") and is_secret_key(normalized)


def secret_values(value: Any, *, sensitive: bool = False) -> set[str]:
    """Collect configured secret values without assuming a concrete service."""
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        value = model_dump()
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, Any]", value)
        values: set[str] = set()
        for key, child in mapping.items():
            child_key = str(key)
            values.update(
                secret_values(
                    child,
                    sensitive=sensitive
                    or child_key.lower() == "headers"
                    or _is_secret_field(child_key),
                )
            )
        return values
    if isinstance(value, (list, tuple)):
        sequence = cast("list[Any] | tuple[Any, ...]", value)
        sequence_values: set[str] = set()
        for child in sequence:
            sequence_values.update(secret_values(child, sensitive=sensitive))
        return sequence_values
    return {value} if sensitive and isinstance(value, str) and value else set()


@contextmanager
def redact_operation_logs(service: Any, *, extra_secrets: Iterable[str] = ()) -> Generator[None]:
    """Protect unsaved credentials while plugins and HTTP clients perform an operation."""
    from mailflow.logging import SecretRedactionFilter, get_active_runtime

    secrets = secret_values(getattr(service, "config", None))
    secrets.update(secret for secret in extra_secrets if secret)
    ordered = sorted(secrets, key=len, reverse=True)
    runtime = get_active_runtime()
    if runtime is not None:
        for secret in ordered:
            runtime.add_secret(secret)
    redactor = SecretRedactionFilter(ordered)
    loggers = [logging.getLogger(name) for name in ("", "mailflow", "httpx", "httpcore")]
    targets: list[Any] = [*loggers, *{handler for log in loggers for handler in log.handlers}]
    for target in targets:
        target.addFilter(redactor)
    try:
        yield
    finally:
        for target in targets:
            target.removeFilter(redactor)


def error_detail(
    service: Any,
    error: BaseException | str,
    *,
    max_chars: int | None = None,
    extra_secrets: Iterable[str] = (),
) -> str:
    """Return an error detail with every configured credential redacted."""
    from mailflow.errors import error_text

    detail = error_text(error, service.t) if isinstance(error, BaseException) else error
    secrets = secret_values(getattr(service, "config", None))
    secrets.update(secret for secret in extra_secrets if secret)
    for secret in sorted(secrets, key=len, reverse=True):
        detail = detail.replace(secret, "***")
    return detail[:max_chars] if max_chars is not None else detail


def error_message(
    service: Any,
    error: BaseException | str,
    *,
    max_chars: int | None = None,
    extra_secrets: Iterable[str] = (),
) -> str:
    """Wrap a sanitized backend failure in the active language's UI text."""
    return str(
        service.t(
            "common.error",
            message=error_detail(service, error, max_chars=max_chars, extra_secrets=extra_secrets),
        )
    )


__all__ = [
    "analysis_status",
    "error_detail",
    "error_message",
    "redact_operation_logs",
    "relabel_buttons",
    "secret_values",
    "urgency_label",
]
