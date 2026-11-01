"""Switching general.language hot-rebuilds the pipeline so the LLM
processor's summary language follows the UI language immediately."""

from __future__ import annotations

from typing import Any, cast

import pytest
from mailflow.config import LLMConfig, MailFlowConfig
from mailflow.contracts import LLMCompletion
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine
from mailflow.plugin_api import define_plugin
from mailflow.plugins import PluginManager
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


def fake_manager(config: MailFlowConfig | None = None) -> PluginManager:
    class Backend:
        backend_id = "language-test"

        def __init__(self, config: LLMConfig) -> None:
            pass

        async def chat(
            self, messages: Any, *, temperature: Any = None, options: Any = None, tools: Any = None
        ) -> LLMCompletion:
            return LLMCompletion(text='{"summary": "local fake", "urgency": "info"}')

    plugin = define_plugin("mailflow-language-test")
    plugin.llm("language-test")(Backend)
    manager = PluginManager(config)
    manager.register(plugin.build())
    return manager


@pytest.fixture
def service(tmp_path: Any) -> MailFlowService:
    manager = fake_manager()
    svc = MailFlowService(
        config=MailFlowConfig(),
        registry=manager.build_registry(),
        plugin_manager=manager,
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
    # an llm-importance entry exists as soon as the user configures an LLM
    from mailflow.config import LLMConfig, ProcessorConfig
    from mailflow.llm import LLMRouterImpl
    from mailflow.processors import register_builtin_processors
    from mailflow.service import _build_processors  # pyright: ignore[reportPrivateUsage]

    register_builtin_processors(service.registry)
    service.config.llms = [LLMConfig(llm_id="main", provider="language-test", model="local")]
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
    for binding in service.pipeline._bindings:  # pyright: ignore[reportPrivateUsage]
        options = binding.options or {}
        if binding.processor_id == "llm-importance":
            return str(options.get("language", ""))
    return ""


async def test_startup_applies_persisted_language_to_pipeline(tmp_path: Any) -> None:
    """A fresh service start must build the pipeline with the persisted UI
    language — not the [i18n] bootstrap default (regression: zh-CN
    preference with an en bootstrap produced English summaries)."""
    from pathlib import Path

    from mailflow.service import start_service
    from mailflow_storage_sqlite.plugin import plugin as storage_plugin

    def build_config(db: Path) -> Any:
        from mailflow.config import LLMConfig, MailFlowConfig, ProcessorConfig

        cfg = MailFlowConfig()
        cfg.storage.path = str(db)
        cfg.i18n.language = "en"  # bootstrap default, as in example configs
        cfg.general.language = "zh-CN"
        cfg.llms = [LLMConfig(llm_id="main", provider="language-test", model="local")]
        cfg.processors = [
            ProcessorConfig(processor_id="llm-importance", provider="llm-importance", llm="main")
        ]
        return cfg

    config_path = tmp_path / "cfg.toml"
    config_path.write_text("", encoding="utf-8")

    # first run: persist the zh-CN preference the way the TUI does
    config = build_config(tmp_path / "language.db")
    manager = fake_manager(config)
    manager.register(storage_plugin)
    service = await start_service(
        config,
        config_path=config_path,
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    await service.set_language("zh-CN")
    await service.stop()

    # second run: the persisted preference must reach the pipeline
    service2 = await start_service(
        build_config(tmp_path / "language.db"),
        config_path=config_path,
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )
    try:
        assert await service2.get_language() == "zh-CN"
        assert _pipeline_language(service2) == "zh-CN"
    finally:
        await service2.stop()
