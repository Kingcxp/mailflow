"""Canned processor phrases are stored as English data; display_text
localizes them at render time without rewriting records."""

from __future__ import annotations

from typing import Any, cast

from mailflow.config import MailFlowConfig
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine
from mailflow.plugins import PluginManager
from mailflow.registry import ComponentRegistry
from mailflow.service import MailFlowService


def _service(language: str) -> MailFlowService:
    return MailFlowService(
        config=MailFlowConfig(),
        registry=ComponentRegistry(),
        plugin_manager=PluginManager(),
        storage=cast(Any, object()),
        sources={},
        router=cast(Any, None),
        pipeline=PipelineEngine([]),
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        i18n=I18n(language),
    )


def test_canned_phrases_localize_per_language() -> None:
    zh = _service("zh-CN")
    assert zh.display_text("Advertisement detected by rules") == "规则判定为广告"
    assert zh.display_text("matches advertising keywords") == "命中广告关键词"
    assert zh.display_text("sender is on the important-senders list") == "发件人在重要发件人名单中"
    en = _service("en")
    assert en.display_text("Advertisement detected by rules") == "Advertisement detected by rules"


def test_free_text_passes_through_untouched() -> None:
    zh = _service("zh-CN")
    assert zh.display_text("领学生证需要本人到场") == "领学生证需要本人到场"
    assert zh.display_text(None) == ""
    assert zh.display_text("") == ""
