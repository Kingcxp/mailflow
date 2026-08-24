"""Switching general.language hot-rebuilds the pipeline so the LLM
processor's summary language follows the UI language immediately."""

from __future__ import annotations

from typing import Any, cast

import pytest
from mailflow.config import MailFlowConfig
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine
from mailflow.plugins import PluginManager
from mailflow.registry import ComponentRegistry
from mailflow.service import MailFlowService


class _Store:
    def __init__(self) -> None:
        self.preferences: dict[str, str] = {}

    async def initialize(self) -> None: ...
    async def close(self) -> None: ...
    async def get_preference(self, k: str) -> str | None:
        return self.preferences.get(k)

    async def set_preference(self, k: str, v: str) -> None:
        self.preferences[k] = v


@pytest.fixture
def service(tmp_path: Any) -> MailFlowService:
    svc = MailFlowService(
        config=MailFlowConfig(),
        registry=ComponentRegistry(),
        plugin_manager=PluginManager(),
        storage=cast(Any, _Store()),
        sources={},
        router=cast(Any, None),
        pipeline=PipelineEngine([]),
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        i18n=I18n(),
    )
    return svc


async def test_language_switch_updates_pipeline_language(service: MailFlowService) -> None:
    from mailflow.llm import LLMRouterImpl
    from mailflow.processors import register_builtin_processors
    from mailflow.service import _build_processors

    register_builtin_processors(service.registry)

    # an llm-importance entry exists as soon as the user configures an LLM
    from mailflow.config import LLMConfig, ProcessorConfig

    service.config.llms = [
        LLMConfig(llm_id="main", provider="openai-completions", model="gpt-test")
    ]
    service.config.processors = [
        ProcessorConfig(processor_id="llm-importance", provider="llm-importance", llm="main")
    ]

    # seed a pipeline the way start_service does
    service.pipeline = _build_processors(
        service.config, service.registry, LLMRouterImpl({}, {}), language="en"
    )

    assert _pipeline_language(service) == "en"

    await service.set_setting("general.language", "zh-CN")

    assert service.i18n.language == "zh-CN"
    assert _pipeline_language(service) == "zh-CN"


def _pipeline_language(service: MailFlowService) -> str:
    """The language option baked into the llm-importance binding."""
    for binding in service.pipeline._bindings:
        options = binding.options or {}
        if binding.processor_id == "llm-importance":
            return str(options.get("language", ""))
    return ""
