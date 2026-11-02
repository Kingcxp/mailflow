"""Headless UI fixtures with fake external mail and model services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mailflow.config import LLMConfig, MailAccountConfig, MailFlowConfig
from mailflow.contracts import MailSource
from mailflow.domain import ComponentKind
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar
from mailflow_testkit.fakes import FakeMailSource, make_mail
from textual.widgets import (
    Static,
)

AD_JSON = """{
  "summary": "Special promotion inside",
  "urgency": "ad",
  "reason": "",
  "reply_required": false,
  "suggested_reply": "",
  "action_items": [],
  "notes": ""
}"""

INFO_JSON = """{
  "summary": "Lecture on Friday is optional",
  "urgency": "info",
  "reason": "",
  "reply_required": false,
  "suggested_reply": "",
  "action_items": [],
  "notes": ""
}"""

ID_DUE = (datetime.now(UTC) + timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0)

URGENT_JSON = """{
  "summary": "Pick up student ID card today",
  "urgency": "urgent",
  "reason": "must be collected by 17:00",
  "reply_required": true,
  "suggested_reply": "I will come before 17:00.",
  "action_items": [
    {"summary": "Collect student ID", "action_type": "errand",
     "must_do": true,
     "due_at": "@DUE@", "due_end": null,
     "notes": "Bring your own ID photo"}
  ],
  "notes": ""
}""".replace("@DUE@", ID_DUE.isoformat())


class MapLLM:
    """Returns canned JSON depending on the mail subject."""

    backend_id = "test-llm"

    def __init__(self, llm_config: LLMConfig) -> None:
        self.llm_config = llm_config

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any):
        from mailflow.contracts import LLMCompletion

        subject = next((m["content"] for m in messages if m["role"] == "user"), "")
        if "promotion" in subject:
            text = AD_JSON
        elif "ID card" in subject:
            text = URGENT_JSON
        else:
            text = INFO_JSON
        return LLMCompletion(text=text, model="m1")


class TUIPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PluginInfo(
            plugin_id="mailflow-tui-test",
            name="TUI Test Components",
            version="0.0.0",
            kinds=[ComponentKind.MAIL_SOURCE, ComponentKind.LLM_BACKEND],
        )

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        def source_factory(account: MailAccountConfig) -> MailSource:
            return FakeMailSource(
                [
                    make_mail(
                        message_id="m-ad",
                        account_id=account.account_id,
                        subject="Huge promotion sale",
                        body_text="unsubscribe now",
                    ),
                    make_mail(
                        message_id="m-id",
                        account_id=account.account_id,
                        subject="Pick up your student ID card",
                        body_text="Please collect your student ID card at the office before 17:00.",
                    ),
                    make_mail(
                        message_id="m-info",
                        account_id=account.account_id,
                        subject="Optional Friday lecture",
                        body_text="The guest lecture is optional.",
                    ),
                ]
            )

        registrar.add_source("test-source", source_factory)
        registrar.add_llm("test-llm", MapLLM)


def build_config(db_path: Path) -> MailFlowConfig:
    return MailFlowConfig.model_validate(
        {
            "general": {"timezone": "Asia/Shanghai", "workers": 2},
            "storage": {"provider": "sqlite", "path": str(db_path)},
            "plugins": {"repositories": []},
            "accounts": [
                {"account_id": "acct-1", "provider": "test-source", "email": "me@example.com"}
            ],
            "llms": [
                {
                    "llm_id": "llm1",
                    "provider": "test-llm",
                    "model": "m1",
                    "api_key": "sk-tui-secret",
                }
            ],
            "processors": [
                {"processor_id": "rules", "provider": "rules", "priority": 10},
                {
                    "processor_id": "llm-importance",
                    "provider": "llm-importance",
                    "priority": 20,
                    "llm": "llm1",
                },
            ],
            "notifiers": [
                {"notifier_id": "console", "provider": "console", "minimum_urgency": "important"}
            ],
            "logging": {"console": False, "file": False, "jsonl": False},
        }
    )


def find_settings_status(app: Any) -> Any:
    try:
        return app.query_one("#settings-status", Static)
    except Exception:
        return None


def find_mail_ids(messages: list[Any]) -> list[str]:
    """Mail ids the last ``find_mail`` tool result reported to the model."""
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        text = str(message.get("content") or "")
        index = text.rfind("ids: ")
        if index == -1:
            continue
        return [
            part.strip() for part in text[index + 5 :].splitlines()[0].split(",") if part.strip()
        ]
    return []


async def set_select_value(pilot: Any, select: Any, value: str) -> None:
    """Assign a Select value, retrying once around the Textual render race
    where a programmatic assignment can hit the widget before its internal
    label node exists (intermittent '#label' NoMatches on Python 3.12)."""
    from textual.css.query import QueryError

    for attempt in range(10):
        try:
            select.value = value
            await pilot.pause(0.15)
            return
        except QueryError:
            if attempt == 9:
                raise
            await pilot.pause(0.2)
