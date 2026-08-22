"""Hot-reload semantics: plugin enable/disable and list edits apply to the
running runtime without a restart."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from mailflow.config import MailFlowConfig, NotifierConfig
from mailflow.contracts import LLMCompletion, MessageDict
from mailflow.domain import ComponentKind
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine
from mailflow.plugins import PluginInfo, PluginManager
from mailflow.registry import PluginRegistrar
from mailflow.service import MailFlowService


class BeepNotifier:
    backend_id = "beep"

    def __init__(self, config: Any) -> None:
        self._config = config

    async def notify(self, record: Any) -> None:
        return None


class BeepPlugin:
    """Registers one notifier component owned by 'mailflow-notify-beep'."""

    def mailflow_plugin_info(self) -> PluginInfo:
        return PluginInfo(
            plugin_id="mailflow-notify-beep",
            name="Beep",
            version="0.0.1",
            kinds=[ComponentKind.NOTIFIER],
        )

    def mailflow_register(self, registrar: PluginRegistrar, config: MailFlowConfig) -> None:
        registrar.add_notifier("beep", BeepNotifier)


class _PrefStorage:
    def __init__(self) -> None:
        self.preferences: dict[str, str] = {}

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def get_preference(self, key: str) -> str | None:
        return self.preferences.get(key)

    async def set_preference(self, key: str, value: str) -> None:
        self.preferences[key] = value


def make_service(tmp_path: Path) -> MailFlowService:
    config = MailFlowConfig()
    config.notifiers.append(NotifierConfig(notifier_id="console", provider="console"))
    manager = PluginManager(config)
    manager.register(BeepPlugin())
    service = MailFlowService(
        config=config,
        registry=manager.build_registry(),
        plugin_manager=manager,
        storage=cast(Any, _PrefStorage()),
        sources={},
        router=cast(Any, None),
        pipeline=PipelineEngine([]),
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        i18n=I18n(),
    )
    service.config_path = tmp_path / "config.toml"
    return service


async def test_enable_creates_notifier_instance_and_hot_loads(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    await service.storage.initialize()
    created = await service.plugin_enable("mailflow-notify-beep")
    assert created == "beep"
    # the instance was appended to the live config ...
    assert any(n.provider == "beep" for n in service.config.notifiers)
    # ... persisted ...
    path = cast(Path, service.config_path)
    assert "provider" in path.read_text(encoding="utf-8")
    # ... and the plugin is now enabled for the running system
    assert service.plugin_status("mailflow-notify-beep") == "enabled"
    await service.stop()


async def test_disable_unloads_immediately_but_keeps_entries(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    await service.storage.initialize()
    await service.plugin_enable("mailflow-notify-beep")
    assert service.plugin_status("mailflow-notify-beep") == "enabled"
    await service.plugin_disable("mailflow-notify-beep")
    assert service.plugin_status("mailflow-notify-beep") == "disabled"
    # config instance kept so re-enabling restores behaviour
    assert any(n.provider == "beep" for n in service.config.notifiers)
    await service.stop()


async def test_adding_first_llm_binds_the_analyzer(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    await service.storage.initialize()
    assert all(p.provider != "llm-importance" for p in service.config.processors)
    await service.add_config_entry(
        "llms",
        {
            "llm_id": "main",
            "model": "m",
            "api_key_env": "",
            "base_url": "https://example.test/v1",
        },
    )
    binding = next((p for p in service.config.processors if p.provider == "llm-importance"), None)
    assert binding is not None
    assert binding.llm == "main"
    assert binding.fallback_llms == []
    await service.stop()


async def test_account_edit_applies_without_restart(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    await service.storage.initialize()
    reloaded: list[str] = []

    async def capture(event: str, **payload: Any) -> None:
        reloaded.append(event)

    service.on("mailflow.runtime.reconfigured", capture)
    await service.add_config_entry(
        "accounts",
        {"account_id": "a1", "provider": "no-such-adapter", "email": "x@example.com"},
    )
    # the account lands in the live config even though its adapter is absent;
    # the runtime flags it as an account error instead of ignoring the edit
    assert any(a.account_id == "a1" for a in service.config.accounts)
    assert service.runtime.account_error("a1") is not None
    assert reloaded == ["mailflow.runtime.reconfigured"]
    await service.stop()


def test_default_chain_is_empty() -> None:
    """No mechanical analysis out of the box: mails without a configured
    LLM are stored unanalyzed instead of getting canned summaries."""
    from mailflow.service import _default_processors  # pyright: ignore[reportPrivateUsage]

    assert _default_processors() == []


def test_completion_type_contract() -> None:
    # guards the contracts imports used across this module's fixtures
    completion = LLMCompletion(text="t", model="m")
    messages: list[MessageDict] = [{"role": "user", "content": "hi"}]
    assert messages[0]["role"] == "user"
    assert completion.text == "t"


def test_plugin_info_kinds_contract() -> None:
    info = BeepPlugin().mailflow_plugin_info()
    assert info.kinds == [ComponentKind.NOTIFIER]
