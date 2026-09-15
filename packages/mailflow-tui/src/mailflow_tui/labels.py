"""Localized labels and credential-safe error presentation for the TUI."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, cast

from mailflow.config import is_secret_key
from mailflow.domain import Urgency

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


def _is_secret_field(key: str) -> bool:
    """Whether a configuration field's values must never reach the UI."""
    normalized = key.replace("-", "_").lower()
    return not normalized.endswith("_env") and is_secret_key(normalized)


def _secret_values(value: Any, *, sensitive: bool = False) -> set[str]:
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
                _secret_values(
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
            sequence_values.update(_secret_values(child, sensitive=sensitive))
        return sequence_values
    return {value} if sensitive and isinstance(value, str) and value else set()


def error_detail(
    service: Any,
    error: BaseException | str,
    *,
    max_chars: int | None = None,
    extra_secrets: Iterable[str] = (),
) -> str:
    """Return an error detail with every configured credential redacted."""
    detail = str(error)
    secrets = _secret_values(getattr(service, "config", None))
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


__all__ = ["error_detail", "error_message", "urgency_label"]
