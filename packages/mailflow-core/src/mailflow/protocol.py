"""JSON values and event frames shared by REST and WebSocket hosts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from mailflow.domain import Attachment


def json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return json_value(value.value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Attachment):
        # Remote views carry attachment metadata; raw MIME bytes remain local
        # for OCR and never enter event or mail JSON responses.
        return {**value.model_dump(exclude={"data"}), "data": None}
    if isinstance(value, BaseModel):
        fields: Any = value.model_dump(mode="python")
        if not isinstance(fields, dict):
            return json_value(fields)
        return {
            name: json_value(getattr(value, name, item))
            for name, item in cast("dict[str, Any]", fields).items()
        }
    if is_dataclass(value) and not isinstance(value, type):
        return json_value(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(json_value(key)): json_value(item)
            for key, item in cast("Mapping[object, Any]", value).items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_value(item) for item in cast("list[Any]", value)]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def event_frame(event: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"event": event, "payload": json_value(payload)}
