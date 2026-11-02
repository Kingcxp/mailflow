"""Contracts at the plugin/host boundary, using only local fake plugins."""

from __future__ import annotations

import sys
from importlib import metadata
from types import ModuleType
from typing import Any

import pytest
from mailflow.config import LLMConfig, MailFlowConfig
from mailflow.contracts import LLMCompletion, MessageDict
from mailflow.domain import ComponentKind
from mailflow.llm import LLMRouterImpl
from mailflow.plugin_api import define_plugin
from mailflow.plugin_validation import validate_result
from mailflow.plugins import PluginInfo, PluginManager
from mailflow.registry import ComponentRegistry, PluginRegistrar
from mailflow_bundled import create_plugin_manager


class FakeBackend:
    backend_id = "fake-contract"

    def __init__(self, config: LLMConfig) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[MessageDict],
        *,
        temperature: float | None = None,
        options: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMCompletion:
        self.calls.append(
            {"messages": messages, "temperature": temperature, "options": options, "tools": tools}
        )
        return LLMCompletion(text="local")


def fake_plugin(plugin_id: str = "mailflow-contract", backend: type[Any] = FakeBackend) -> Any:
    builder = define_plugin(plugin_id)
    builder.llm("fake-contract")(backend)
    return builder.build()


def install_fake_entry_point(monkeypatch: pytest.MonkeyPatch, plugin: Any) -> None:
    module = ModuleType("mailflow_contract_fixture")
    module.plugin = plugin  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    entry = metadata.EntryPoint(
        name=plugin.mailflow_plugin_info().plugin_id,
        value=f"{module.__name__}:plugin",
        group="mailflow.plugins",
    )
    monkeypatch.setattr(metadata, "entry_points", lambda: metadata.EntryPoints([entry]))


async def test_real_router_passes_all_chat_parameters() -> None:
    manager = PluginManager()
    manager.register(fake_plugin())
    config = LLMConfig(llm_id="local", provider="fake-contract")
    backend = manager.build_registry().llm_factory("fake-contract")(config)
    router = LLMRouterImpl({"local": backend}, {"local": config})
    tools = [{"type": "function", "function": {"name": "find_mail"}}]
    result = await router.chat(
        [{"role": "user", "content": "hi"}],
        primary="local",
        temperature=0.3,
        options={"max_tokens": 4},
        tools=tools,
    )
    assert result.text == "local"
    assert result.backend == "fake-contract"
    assert isinstance(backend, FakeBackend)
    assert backend.calls[0]["tools"] == tools
    assert backend.calls[0]["temperature"] == 0.3
    assert backend.calls[0]["options"] == {"max_tokens": 4}


def test_old_chat_signature_is_rejected_before_first_request() -> None:
    class OldBackend:
        async def chat(
            self, messages: Any, *, temperature: Any = None, options: Any = None
        ) -> None:
            pass

    manager = PluginManager()
    manager.register(fake_plugin(backend=OldBackend))
    registry = manager.build_registry()
    assert not registry.has(ComponentKind.LLM_BACKEND, "fake-contract")
    snapshot = manager.snapshots(registry)[0]
    assert snapshot.status == "error"
    assert "tools" in snapshot.error and "mailflow-contract" in snapshot.error


def test_registration_is_atomic_on_component_conflict() -> None:
    manager = PluginManager()
    manager.register(fake_plugin("first"))

    class ConflictingPlugin:
        def mailflow_plugin_info(self) -> PluginInfo:
            return PluginInfo("conflicting", kinds=[ComponentKind.LLM_BACKEND])

        def mailflow_register(self, registrar: PluginRegistrar, config: MailFlowConfig) -> None:
            registrar.add_llm("new-before-conflict", FakeBackend)
            registrar.add_llm("fake-contract", FakeBackend)

    manager.register(ConflictingPlugin())
    registry = manager.build_registry()
    assert registry.component_ids(ComponentKind.LLM_BACKEND) == ["fake-contract"]
    assert registry.plugin_for("fake-contract", ComponentKind.LLM_BACKEND) == "first"
    assert manager.status("conflicting") == "error"


def test_declared_capabilities_must_match_registration() -> None:
    class WrongCapabilities:
        def mailflow_plugin_info(self) -> PluginInfo:
            return PluginInfo("wrong-kinds", kinds=[ComponentKind.NOTIFIER])

        def mailflow_register(self, registrar: PluginRegistrar, config: MailFlowConfig) -> None:
            registrar.add_llm("fake-contract", FakeBackend)

    manager = PluginManager()
    manager.register(WrongCapabilities())
    assert manager.build_registry().snapshots() == []
    assert "declared kinds" in manager.failures["wrong-kinds"]


def test_factory_return_error_identifies_owner() -> None:
    registry = ComponentRegistry()
    registrar = PluginRegistrar(registry, MailFlowConfig(), "broken-factory")
    registrar.add_llm("wrong-result", lambda config: None)  # type: ignore[arg-type,return-value]
    with pytest.raises(TypeError, match=r"broken-factory.*wrong-result"):
        registry.llm_factory("wrong-result")(LLMConfig(llm_id="test"))


def test_async_factory_is_rejected_before_creating_coroutine() -> None:
    async def create_backend(config: LLMConfig) -> FakeBackend:
        return FakeBackend(config)

    registry = ComponentRegistry()
    registrar = PluginRegistrar(registry, MailFlowConfig(), "async-factory")
    with pytest.raises(TypeError, match=r"async-factory.*synchronous"):
        registrar.add_llm("async-backend", create_backend)  # type: ignore[arg-type]
    assert registry.snapshots() == []


@pytest.mark.parametrize(
    ("kind", "method", "result"),
    [
        (ComponentKind.LLM_BACKEND, "chat", {"text": "wrong"}),
        (ComponentKind.MAIL_PROCESSOR, "process", None),
        (ComponentKind.LLM_ENHANCER, "extra_messages", [{"content": 3}]),
        (ComponentKind.MAIL_SOURCE, "fetch_history", ["wrong"]),
        (ComponentKind.NOTIFIER, "notify", "wrong"),
        (ComponentKind.STORAGE, "list_mails", ["wrong"]),
        (ComponentKind.GATEWAY_PROVISIONER, "status", {"status": "running"}),
        (ComponentKind.BOT_EXPORTER, "export", {"created": []}),
    ],
)
def test_sample_results_are_checked(kind: ComponentKind, method: str, result: Any) -> None:
    with pytest.raises(TypeError):
        validate_result(kind, method, result)


def test_external_discovery_and_disabled_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_entry_point(monkeypatch, fake_plugin())
    for compose in (PluginManager, create_plugin_manager):
        manager = compose(MailFlowConfig())
        manager.discover()
        assert manager.build_registry().has(ComponentKind.LLM_BACKEND, "fake-contract")
        config = MailFlowConfig.model_validate({"plugins": {"disabled": ["mailflow-contract"]}})
        restarted = compose(config)
        restarted.discover()
        assert not restarted.build_registry().has(ComponentKind.LLM_BACKEND, "fake-contract")
        assert restarted.status("mailflow-contract") == "disabled"


def test_disabled_bundled_plugin_reloads_without_entry_point() -> None:
    config = MailFlowConfig.model_validate({"plugins": {"disabled": ["mailflow-contract"]}})
    manager = PluginManager(config)
    imports: list[str] = []

    def load() -> Any:
        imports.append("loaded")
        return fake_plugin()

    manager.load("mailflow-contract", load, bundled=True)
    assert imports == []
    updated = config.model_copy(deep=True)
    updated.plugins.disabled.clear()
    assert manager.build_registry(updated).has(ComponentKind.LLM_BACKEND, "fake-contract")
    assert imports == ["loaded"]


def test_dependency_failure_is_visible_and_disabled_plugin_is_not_imported() -> None:
    manager = PluginManager()

    def missing_dependency() -> Any:
        raise ModuleNotFoundError("No module named 'missing_dependency'")

    manager.load("mailflow-needs-dependency", missing_dependency)
    registry = manager.build_registry()
    snapshot = manager.snapshots(registry)[0]
    assert snapshot.plugin_id == "mailflow-needs-dependency"
    assert snapshot.status == "error" and "missing_dependency" in snapshot.error
    config = MailFlowConfig.model_validate({"plugins": {"disabled": [snapshot.plugin_id]}})
    disabled = PluginManager(config)
    disabled.load(snapshot.plugin_id, lambda: pytest.fail("disabled plugin was imported"))
    assert disabled.build_registry().snapshots() == []
    assert disabled.failures == {}


def test_bundled_implementation_wins_over_duplicate_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_entry_point(monkeypatch, fake_plugin())
    manager = PluginManager()
    manager.register(fake_plugin(), bundled=True)
    manager.discover()
    assert manager.plugin_ids == ["mailflow-contract"]
    registry = manager.build_registry()
    assert len(registry.snapshots()) == 1
    assert manager.failures == {}
