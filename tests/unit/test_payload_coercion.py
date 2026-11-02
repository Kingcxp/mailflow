"""Malformed model output is checked through the public processor contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from mailflow.config import ProcessorConfig
from mailflow.contracts import LLMCompletion, LLMRouter, ProcessingContext
from mailflow.domain import Urgency
from mailflow.processors import LLMImportanceProcessor
from mailflow_testkit.fakes import make_mail


async def analyze(payload: dict[str, Any]) -> Any:
    class ScriptedRouter:
        async def chat(self, *args: Any, **kwargs: Any) -> LLMCompletion:
            return LLMCompletion(text=json.dumps(payload))

    processor = LLMImportanceProcessor(
        ProcessorConfig(processor_id="analysis", provider="llm-importance", llm="fake"),
        cast(LLMRouter, ScriptedRouter()),
    )
    context = ProcessingContext(
        account_id="acct-1", timezone="UTC", now=datetime(2026, 6, 1, tzinfo=UTC)
    )
    result = await processor.process(make_mail(subject="Fallback subject"), context)
    assert result.analysis is not None
    return result.analysis


async def test_null_and_numeric_fields_keep_a_usable_analysis() -> None:
    analysis = await analyze(
        {
            "summary": None,
            "urgency": 2,
            "reason": None,
            "reply_required": "true",
            "suggested_reply": None,
            "notes": None,
            "action_items": [],
        }
    )
    assert analysis.summary == "Fallback subject"
    assert analysis.urgency is Urgency.INFO
    assert analysis.reply_required is True
    assert analysis.reason == ""


async def test_malformed_action_items_do_not_drop_the_valid_item() -> None:
    analysis = await analyze(
        {
            "summary": "Retained summary",
            "urgency": "urgent",
            "action_items": [
                "not a dict",
                {
                    "summary": "Collect papers",
                    "action_type": "errand",
                    "must_do": True,
                    "due_at": "2026-06-10T09:00:00Z",
                },
                42,
                {"summary": "Undated item", "must_do": True, "due_at": None},
            ],
        }
    )
    assert analysis.summary == "Retained summary"
    assert [item.summary for item in analysis.action_items] == ["Collect papers"]
    assert analysis.action_items[0].due_at == datetime(2026, 6, 10, 9, tzinfo=UTC)


@pytest.mark.parametrize("items", [{"oops": 1}, None, "not a list"])
async def test_non_list_actions_do_not_fail_mail_analysis(items: Any) -> None:
    analysis = await analyze({"summary": "Kept", "urgency": "info", "action_items": items})
    assert analysis.summary == "Kept"
    assert analysis.action_items == []


async def test_only_explicit_required_actions_are_scheduled() -> None:
    analysis = await analyze(
        {
            "summary": "Notice",
            "action_items": [
                {"summary": "required", "due_at": "2026-06-10T09:00:00Z", "must_do": "yes"},
                {"summary": "missing", "due_at": "2026-06-10T09:00:00Z"},
                {"summary": "optional", "due_at": "2026-06-10T09:00:00Z", "must_do": "false"},
            ],
        }
    )
    assert [item.summary for item in analysis.action_items] == ["required"]
