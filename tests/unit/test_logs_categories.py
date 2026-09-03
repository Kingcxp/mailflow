"""LogsPane source categorization: raw logger names map to user-facing
categories (localized in the UI), so the source filter shows meaningful
groups instead of internal logger plumbing."""

from __future__ import annotations

from mailflow_tui.app import LogsPane


def _category(logger_name: str) -> str:
    return LogsPane._category_of(logger_name)  # pyright: ignore[reportPrivateUsage]


def test_chat_sources_map_to_chat() -> None:
    assert _category("mailflow.bot_server") == "chat"
    assert _category("mailflow.gateway.napcat") == "chat"
    assert _category("mailflow.plugins.openwechat") == "chat"


def test_mail_and_llm_sources() -> None:
    assert _category("mailflow.service") == "mail"
    assert _category("mailflow.llm") == "llm"
    assert _category("mailflow.llm.openai") == "llm"


def test_pipeline_and_notify() -> None:
    assert _category("mailflow.pipeline") == "parse"
    assert _category("mailflow.processor") == "parse"
    assert _category("mailflow.notify.console") == "notify"
    assert _category("mailflow.reminder") == "notify"


def test_storage_and_unknown() -> None:
    assert _category("mailflow.storage.sqlite") == "storage"
    assert _category("mailflow.updates") == "system"
    assert _category("other.framework") == "system"
