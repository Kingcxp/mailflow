"""Localized presentation labels for persisted domain values."""

from __future__ import annotations

from typing import Any

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


__all__ = ["urgency_label"]
