"""Declarative plugin API: decorators build the two pluggy hooks."""

from __future__ import annotations

from typing import Any

from mailflow.config import MailFlowConfig
from mailflow.domain import ComponentKind
from mailflow.plugin_api import define_plugin
from mailflow.registry import ComponentRegistry, PluginRegistrar


def _register(plugin: object) -> ComponentRegistry:
    registrar = PluginRegistrar(ComponentRegistry(), MailFlowConfig(), "test")
    plugin.mailflow_register(registrar, MailFlowConfig())  # type: ignore[attr-defined]
    return registrar._registry  # pyright: ignore[reportPrivateUsage]


def test_multi_component_plugin() -> None:
    PLUGIN = define_plugin(
        "mailflow-demo-bundle",
        name="Demo Bundle",
        version="0.1.0",
        description="one source + one notifier",
    )

    @PLUGIN.source("demo-source")
    class DemoSource:  # pyright: ignore[reportUnusedClass]
        async def run(self, emit: Any, stop_event: Any) -> None:
            await stop_event.wait()

        async def send_reply(self, mail_id: str, draft: object) -> None:
            pass

        async def close(self) -> None:
            pass

    @PLUGIN.notifier("demo-notify")
    class DemoNotifier:  # pyright: ignore[reportUnusedClass]
        async def notify(self, record: object) -> None:
            pass

    plugin = PLUGIN.build()
    info = plugin.mailflow_plugin_info()  # type: ignore[attr-defined]
    assert info.plugin_id == "mailflow-demo-bundle"
    assert set(info.kinds) == {ComponentKind.MAIL_SOURCE, ComponentKind.NOTIFIER}

    registry = _register(plugin)
    assert registry.source_factory("demo-source") is not None
    assert registry.notifier_factory("demo-notify") is not None
    assert registry.plugin_for("demo-source") == "test"


def test_component_id_from_class_attribute() -> None:
    PLUGIN = define_plugin("mailflow-demo-llm", name="Demo LLM")

    @PLUGIN.llm()
    class DemoBackend:  # pyright: ignore[reportUnusedClass]
        backend_id = "demo-llm"

        async def chat(self, messages: list[dict[str, str]], **kwargs: object) -> object:
            raise NotImplementedError

    registry = _register(PLUGIN.build())
    assert registry.llm_factory("demo-llm") is not None


def test_all_kinds_registered() -> None:
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
