"""Declarative plugin API: decorators build the two pluggy hooks."""

from __future__ import annotations

import asyncio
from typing import Any

from mailflow.config import LLMConfig, MailAccountConfig, MailFlowConfig, NotifierConfig
from mailflow.contracts import LLMCompletion
from mailflow.domain import ComponentKind, MailAnalysis, MailRecord, Urgency
from mailflow.plugin_api import define_plugin
from mailflow.registry import ComponentRegistry, PluginRegistrar
from mailflow_testkit.fakes import FakeMailSource, FakeNotifier, make_mail


def _register(plugin: object) -> ComponentRegistry:
    registry = ComponentRegistry()
    registrar = PluginRegistrar(registry, MailFlowConfig(), "test")
    plugin.mailflow_register(registrar, MailFlowConfig())  # type: ignore[attr-defined]
    return registry


async def test_multi_component_plugin_delivers_through_registered_factories() -> None:
    PLUGIN = define_plugin(
        "mailflow-demo-bundle",
        name="Demo Bundle",
        version="0.1.0",
        description="one source + one notifier",
    )

    class DemoSource(FakeMailSource):
        def __init__(self, config: MailAccountConfig) -> None:
            super().__init__([make_mail(account_id=config.account_id)])

    class DemoNotifier(FakeNotifier):
        def __init__(self, config: NotifierConfig) -> None:
            super().__init__()

    PLUGIN.source("demo-source")(DemoSource)
    PLUGIN.notifier("demo-notify")(DemoNotifier)
    plugin = PLUGIN.build()
    info = plugin.mailflow_plugin_info()  # type: ignore[attr-defined]
    assert info.plugin_id == "mailflow-demo-bundle"
    assert set(info.kinds) == {ComponentKind.MAIL_SOURCE, ComponentKind.NOTIFIER}

    registry = _register(plugin)
    source = registry.source_factory("demo-source")(
        MailAccountConfig(account_id="demo", provider="demo-source")
    )
    notifier = registry.notifier_factory("demo-notify")(
        NotifierConfig(notifier_id="demo", provider="demo-notify")
    )
    delivered: list[MailRecord] = []
    stop = asyncio.Event()

    async def emit(mail: Any) -> None:
        record = MailRecord(
            record_id=mail.normalized_message_id(),
            mail=mail,
            auto_urgency=Urgency.INFO,
            analysis=MailAnalysis(summary="kept", urgency=Urgency.INFO),
        )
        await notifier.notify(record)
        delivered.append(record)
        stop.set()

    await source.run(emit, stop)
    assert isinstance(notifier, DemoNotifier)
    assert notifier.notified == delivered and len(delivered) == 1
    assert delivered[0].mail.account_id == "demo"
    assert registry.plugin_for("demo-source") == "test"


async def test_inferred_component_id_builds_a_working_backend() -> None:
    PLUGIN = define_plugin("mailflow-demo-llm", name="Demo LLM")

    @PLUGIN.llm()
    class DemoBackend:  # pyright: ignore[reportUnusedClass]
        backend_id = "demo-llm"

        def __init__(self, config: LLMConfig) -> None:
            self.model = config.model

        async def chat(self, messages: list[dict[str, str]], **kwargs: object) -> LLMCompletion:
            return LLMCompletion(text=messages[-1]["content"], model=self.model)

    registry = _register(PLUGIN.build())
    backend = registry.llm_factory("demo-llm")(LLMConfig(llm_id="demo", model="fixture-model"))
    result = await backend.chat([{"role": "user", "content": "fixture request"}])
    assert result.text == "fixture request" and result.model == "fixture-model"


def test_decorators_record_declared_component_kinds() -> None:
    PLUGIN = define_plugin("mailflow-demo-all", name="Demo All")

    @PLUGIN.processor("p")
    class P:  # pyright: ignore[reportUnusedClass]
        processor_id = "p"

        async def process(self, mail: object, context: object) -> object:
            raise NotImplementedError

    @PLUGIN.storage("s")
    class S:  # pyright: ignore[reportUnusedClass]
        async def initialize(self) -> None:
            pass

    @PLUGIN.bot_exporter("demo-bot")
    class E:  # pyright: ignore[reportUnusedClass]
        def export(self, context: object) -> object:
            raise NotImplementedError

    info = PLUGIN.build().mailflow_plugin_info()  # type: ignore[attr-defined]
    assert {k.value for k in info.kinds} == {
        "mail_processor",
        "storage",
        "bot_exporter",
    }
