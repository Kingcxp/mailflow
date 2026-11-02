"""Hot-reload semantics: plugin enable/disable and list edits apply to the
running runtime without a restart."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from mailflow.config import MailFlowConfig, NotifierConfig, load_config
from mailflow.domain import ComponentKind
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine
from mailflow.plugins import PluginInfo, PluginManager
from mailflow.registry import PluginRegistrar
from mailflow.service import MailFlowService

from mailflow_test_support.commands import MemoryStorage


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


def make_service(tmp_path: Path) -> MailFlowService:
    config = MailFlowConfig()
    config.notifiers.append(NotifierConfig(notifier_id="console", provider="console"))
    manager = PluginManager(config)
    manager.register(BeepPlugin())
    service = MailFlowService(
        config=config,
        registry=manager.build_registry(),
        plugin_manager=manager,
        storage=cast(Any, MemoryStorage()),
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


async def test_failed_live_reload_stops_old_runtime_and_reports_saved_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    service = make_service(tmp_path)
    bot = SimpleNamespace(url="http://fake/bot", start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr("mailflow.bot_server.BotServer", Mock(return_value=bot))
    await service.start()
    tasks = asyncio.all_tasks() - {asyncio.current_task()}
    build_registry = service.plugin_manager.build_registry
    monkeypatch.setattr(
        service.plugin_manager, "build_registry", Mock(side_effect=RuntimeError("bad plugin"))
    )
    with pytest.raises(RuntimeError, match="reload failed"):
        await service.set_setting("general.workers", 4)
    assert load_config(str(service.config_path)).general.workers == 4
    assert not service.started
    assert all(task.done() for task in tasks)
    bot.stop.assert_awaited_once()
    monkeypatch.setattr(service.plugin_manager, "build_registry", build_registry)
    await service.start()
    assert service.started
    await service.stop()


async def test_service_retries_failed_gateway_shutdown(tmp_path: Path) -> None:
    from types import SimpleNamespace

    service = make_service(tmp_path)
    stop = AsyncMock(side_effect=[RuntimeError("busy"), None])
    service.gateways = cast(Any, SimpleNamespace(stop=stop))
    await service.stop()
    await service.stop()
    await service.stop()
    assert stop.await_count == 2


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


async def test_service_start_failure_rolls_back_and_can_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service(tmp_path)
    service.config.general.auto_update = False
    close = AsyncMock(wraps=service.storage.close)
    monkeypatch.setattr(service.storage, "close", close)
    fail_start = True
    bots: list[Any] = []

    class Bot:
        url = "http://fake/bot"

        def __init__(self, owner: Any) -> None:
            self.stopped = 0
            bots.append(self)

        async def start(self) -> None:
            if fail_start:
                raise RuntimeError("port occupied")

        async def stop(self) -> None:
            self.stopped += 1

    monkeypatch.setattr("mailflow.bot_server.BotServer", Bot)
    with pytest.raises(RuntimeError, match="port occupied"):
        await service.start()
    assert not service.started
    assert bots[0].stopped == 1
    assert close.await_count == 1
    assert service.runtime._tasks == []  # pyright: ignore[reportPrivateUsage]
    fail_start = False
    await service.start()
    first = list(service.runtime._tasks)  # pyright: ignore[reportPrivateUsage]
    await service.start()
    assert len(bots) == 2
    assert service.runtime._tasks == first  # pyright: ignore[reportPrivateUsage]
    await service.stop()
    await service.stop()
    assert close.await_count == 2
    assert bots[1].stopped == 1
    assert all(task.done() for task in first)


async def test_live_reload_releases_old_gateway_and_schedulers_before_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service(tmp_path)
    service.config.general.auto_update = False
    order: list[str] = []

    class Gateways:
        _service_ref: Any = None

        def __init__(self, *args: Any) -> None:
            self.active = False

        async def start(self) -> None:
            self.active = True
            order.append("gateway-start")

        async def stop(self, **kwargs: Any) -> None:
            self.active = False
            order.append("gateway-stop")

    class Bot:
        url = "http://fake/bot"

        def __init__(self, owner: Any) -> None:
            pass

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    monkeypatch.setattr("mailflow.service.GatewayManager", Gateways)
    monkeypatch.setattr("mailflow.bot_server.BotServer", Bot)
    service.gateways = cast(Any, Gateways())
    await service.start()
    old_gateway = service.gateways
    tasks = [*service.runtime._tasks, service._update_task, service._expiry_task]  # pyright: ignore[reportPrivateUsage]
    order.clear()
    try:
        changed = service.config.model_copy(deep=True)
        changed.general.workers = 4
        changed.general.queue_size = 2
        service.config = changed
        await service.reload_runtime()
        await asyncio.sleep(0)
        assert service.gateways is not old_gateway
        assert order == ["gateway-stop", "gateway-start"]
        assert all(task is None or task.done() for task in tasks)
        assert service.runtime._queue.maxsize == 2  # pyright: ignore[reportPrivateUsage]
        assert (
            len([task for task in asyncio.all_tasks() if task.get_name().startswith("worker-")])
            == 4
        )  # pyright: ignore[reportPrivateUsage]
        assert not service._stopped_event.is_set()  # pyright: ignore[reportPrivateUsage]
    finally:
        await service.stop()


def test_default_chain_uses_rules_without_a_model() -> None:
    from mailflow.service import _default_processors  # pyright: ignore[reportPrivateUsage]

    assert [processor.provider for processor in _default_processors()] == ["rules"]


async def test_disabling_only_allowlisted_plugin_does_not_enable_others(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    plugin_id = "mailflow-notify-beep"
    service.config.plugins.enabled = [plugin_id]
    await service.plugin_disable(plugin_id)
    assert service.config.plugins.enabled == [plugin_id]
    assert not service.registry.has(ComponentKind.NOTIFIER, "beep")
    saved = load_config(str(service.config_path))
    assert saved.plugins.enabled == [plugin_id] and saved.plugins.disabled == [plugin_id]
    restarted = PluginManager(saved)
    restarted.register(BeepPlugin())
    assert not restarted.build_registry().has(ComponentKind.NOTIFIER, "beep")
    await service.plugin_enable(plugin_id)
    assert service.registry.has(ComponentKind.NOTIFIER, "beep")
    assert service.config.plugins.enabled == [plugin_id]
    await service.stop()


async def test_enable_discovers_a_package_installed_since_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from importlib import metadata
    from types import SimpleNamespace

    service = make_service(tmp_path)
    service.plugin_manager.unregister("mailflow-notify-beep")
    entry = SimpleNamespace(name="mailflow-notify-beep", dist=None, load=lambda: BeepPlugin())
    selection = Mock()
    selection.select.return_value = [entry]
    monkeypatch.setattr(metadata, "entry_points", Mock(return_value=selection))
    await service.plugin_enable(entry.name)
    assert service.registry.has(ComponentKind.NOTIFIER, "beep")
    assert service.plugin_status(entry.name) == "enabled"
    await service.stop()


async def test_failed_enable_keeps_saved_disable_choice(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    plugin_id = "mailflow-broken"
    service.config.plugins.disabled = [plugin_id]

    def missing_dependency() -> Any:
        raise ModuleNotFoundError("missing_dependency")

    service.plugin_manager.load(plugin_id, missing_dependency)
    with pytest.raises(RuntimeError, match=r"mailflow-broken.*missing_dependency"):
        await service.plugin_enable(plugin_id)
    assert service.config.plugins.disabled == [plugin_id]
    assert not cast(Path, service.config_path).exists()
    assert service.plugin_status(plugin_id) == "disabled"
    await service.stop()


async def test_invalid_factory_does_not_persist_an_enable(tmp_path: Path) -> None:
    from mailflow.plugin_api import define_plugin

    service = make_service(tmp_path)
    plugin_id = "mailflow-invalid-factory"
    service.config.plugins.disabled = [plugin_id]
    plugin = define_plugin(plugin_id)

    def invalid_factory(config: Any) -> Any:
        return object()

    plugin.notifier("invalid")(invalid_factory)
    service.plugin_manager.register(plugin.build())
    with pytest.raises(TypeError, match="mailflow-invalid-factory"):
        await service.plugin_enable(plugin_id)
    assert service.config.plugins.disabled == [plugin_id]
    assert service.plugin_status(plugin_id) == "disabled"
    assert not cast(Path, service.config_path).exists()
    assert not service.registry.has(ComponentKind.NOTIFIER, "invalid")
    await service.stop()


async def test_uninstall_unloads_components_and_preserves_filter_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mailflow.plugin_market import MarketPlugin, PluginMarket, Repository

    service = make_service(tmp_path)
    plugin_id = "mailflow-notify-beep"
    await service.plugin_enable(plugin_id)
    service.config.plugins.enabled = [plugin_id]
    await service.plugin_disable(plugin_id)
    plugin = MarketPlugin(id=plugin_id, package=plugin_id, source="local")

    class LocalMarket:
        def find(self, key: str) -> tuple[Repository, MarketPlugin]:
            return Repository(name="local", url="local"), plugin

        async def uninstall(self, item: MarketPlugin) -> str:
            return "removed"

    service.market = cast(Any, LocalMarket())
    monkeypatch.setattr(PluginMarket, "is_installed", Mock(return_value=True))
    assert await service.plugin_uninstall(plugin_id) == "removed"
    assert plugin_id not in service.plugin_manager.known_plugin_ids
    assert not any(n.provider == "beep" for n in service.config.notifiers)
    saved = load_config(str(service.config_path))
    assert saved.plugins.enabled == [plugin_id] and saved.plugins.disabled == [plugin_id]
    assert not service.registry.has(ComponentKind.NOTIFIER, "beep")
    await service.stop()


async def test_uninstall_after_disabled_restart_uses_saved_package_and_cleans_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mailflow.plugin_market import MarketPlugin, PluginMarket

    service = make_service(tmp_path)
    plugin_id = "mailflow-notify-beep"
    service.config.plugins.disabled = [plugin_id]
    service.config.notifiers.append(NotifierConfig(notifier_id="beep", provider="beep"))
    restarted = PluginManager(service.config)
    loader = Mock(return_value=BeepPlugin())
    restarted.load(plugin_id, loader)
    service.plugin_manager = restarted
    service.registry = restarted.build_registry()
    loader.assert_not_called()
    await service.storage.set_preference(f"plugin.source.{plugin_id}", "local-folder")
    await service.storage.set_preference(f"plugin.package.{plugin_id}", "beep-distribution")
    lookups: list[str] = []

    class LocalMarket:
        def find(self, key: str) -> None:
            lookups.append(key)

        async def uninstall(self, plugin: MarketPlugin) -> str:
            assert plugin.package == "beep-distribution"
            return "removed"

    service.market = cast(Any, LocalMarket())
    monkeypatch.setattr(PluginMarket, "is_installed", Mock(return_value=True))
    assert await service.plugin_uninstall(plugin_id) == "removed"
    assert lookups == []
    loader.assert_called_once()
    saved = load_config(str(service.config_path))
    assert not any(notifier.provider == "beep" for notifier in saved.notifiers)
    assert saved.plugins.disabled == [plugin_id]
    assert plugin_id not in service.plugin_manager.known_plugin_ids
    await service.stop()
