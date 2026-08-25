"""LLM JSON slips (null summary, string booleans, malformed action items)
are coerced instead of failing the whole mail's analysis."""

from __future__ import annotations

from typing import Any

from mailflow.processors import (
    AnalysisPayload,
    _coerce_analysis_payload,  # pyright: ignore[reportPrivateUsage]
)


def test_null_and_numeric_fields_coerce() -> None:
    raw: dict[str, Any] = {
        "summary": None,
        "urgency": 2,
        "reason": None,
        "reply_required": "true",
        "suggested_reply": None,
        "notes": None,
        "action_items": [],
    }
    payload = AnalysisPayload.model_validate(_coerce_analysis_payload(raw))
    assert payload.summary == ""
    assert payload.urgency == "2"
    assert payload.reply_required is True
    assert payload.reason == ""


def test_malformed_action_items_drop_individually() -> None:
    raw: dict[str, Any] = {
        "summary": "s",
        "urgency": "urgent",
        "action_items": [
            "not a dict",
            {"summary": "领材料", "action_type": "errand", "due_at": None},
            42,
        ],
    }
    payload = AnalysisPayload.model_validate(_coerce_analysis_payload(raw))
    assert len(payload.action_items) == 1
    assert payload.action_items[0].due_at == ""
    assert payload.action_items[0].summary == "领材料"


def test_non_list_action_items_become_empty() -> None:
    raw = {"summary": "s", "urgency": "info", "action_items": {"oops": 1}}
    payload = AnalysisPayload.model_validate(_coerce_analysis_payload(raw))
    assert payload.action_items == []
