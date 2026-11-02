"""Custom exporters and generated bot handlers honor host lifecycle and permissions."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Awaitable, Callable
from importlib import metadata
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from mailflow.bot_export import (
    BotExportContext,
    BotExportResult,
    export_bot_plugin,
)
from mailflow.commands import CommandRouter
from mailflow.config import MailFlowConfig, NotifierConfig
from mailflow.domain import ComponentKind
from mailflow.permissions import PermissionContext, current_context
from mailflow.plugins import PluginInfo
from mailflow.registry import ComponentRegistry, PluginRegistrar
from mailflow.service import MailFlowService
from mailflow_export_astrbot.exporter import export_astrbot
from mailflow_export_nonebot.exporter import export_nonebot

LifecycleHook = Callable[[], Awaitable[None]]


class FakeExporterPlugin:
    """Inline exporter plugin used to exercise the registry path."""

    def mailflow_plugin_info(self) -> PluginInfo:
        return PluginInfo(
            plugin_id="mailflow-export-fake",
            name="Fake Exporter",
            version="0.0.1",
            description="test exporter",
            kinds=[ComponentKind.BOT_EXPORTER],
        )

    def mailflow_register(self, registrar: PluginRegistrar, config: object) -> None:
        registrar.add_bot_exporter("fake", self._export)

    def _export(self, context: BotExportContext) -> BotExportResult:
        target = context.output_dir
        target.mkdir(parents=True, exist_ok=True)
        (target / "README.md").write_text("fake exporter\n", encoding="utf-8")
        return BotExportResult(
            framework="fake",
            plugin_name="fake_plugin",
            created=["README.md"],
            notes=f"plugins={sorted(context.plugin_ids)} version={context.version}",
        )


def _registry_with(plugin: object) -> ComponentRegistry:
    from mailflow.plugins import PluginManager

    manager = PluginManager(MailFlowConfig())
    assert manager.register(plugin) is not None
    return manager.build_registry()


# ---------------------------------------------------------------------------
# Shared export entry point
# ---------------------------------------------------------------------------


class TestExportBotPlugin:
    def test_runs_registered_factory(self, tmp_path: Path) -> None:
        registry = _registry_with(FakeExporterPlugin())
        result = export_bot_plugin(
            registry,
            MailFlowConfig(),
            framework="fake",
            output_dir=tmp_path / "nested" / "export",
            plugin_ids=["mailflow-a", "mailflow-b"],
            version="1.2.3",
            language="zh-CN",
        )
        assert result.framework == "fake"
        assert result.plugin_name == "fake_plugin"
        assert result.created == ["README.md"]
        assert (tmp_path / "nested/export/README.md").is_file()
        assert "plugins=['mailflow-a', 'mailflow-b'] version=1.2.3" in result.notes


# ---------------------------------------------------------------------------
# Built-in exporters
# ---------------------------------------------------------------------------


def _context(output_dir: Path, plugin_ids: list[str] | None = None) -> BotExportContext:
    return BotExportContext(
        config=MailFlowConfig(),
        plugin_ids=plugin_ids or ["mailflow-storage-sqlite", "mailflow-notify-console"],
        output_dir=output_dir,
        version="9.9.9",
        language="en",
    )


class TestNonebotExporter:
    async def test_generated_plugin_lifecycle_discovers_external_plugins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Driver:
            def __init__(self) -> None:
                self.startups: list[LifecycleHook] = []
                self.shutdowns: list[LifecycleHook] = []

            def on_startup(self, hook: LifecycleHook) -> LifecycleHook:
                self.startups.append(hook)
                return hook

            def on_shutdown(self, hook: LifecycleHook) -> LifecycleHook:
                self.shutdowns.append(hook)
                return hook

        driver = Driver()

        def register_handler(
            handler: Callable[..., Awaitable[None]],
        ) -> Callable[..., Awaitable[None]]:
            return handler

        matcher = Mock()
        matcher.handle.return_value = register_handler
        nonebot = ModuleType("nonebot")
        nonebot.__dict__.update(
            get_driver=Mock(return_value=driver), on_message=Mock(return_value=matcher)
        )
        monkeypatch.setitem(sys.modules, "nonebot", nonebot)
        adapters = ModuleType("nonebot.adapters")
        adapters.__dict__.update(Bot=object, Event=object)
        monkeypatch.setitem(sys.modules, "nonebot.adapters", adapters)
        external = ModuleType("mailflow_generated_fixture")
        external.__dict__["plugin"] = FakeExporterPlugin()
        monkeypatch.setitem(sys.modules, external.__name__, external)
        entry = metadata.EntryPoint(
            name="mailflow-export-fake",
            value=f"{external.__name__}:plugin",
            group="mailflow.plugins",
        )
        monkeypatch.setattr(
            metadata, "entry_points", Mock(return_value=metadata.EntryPoints([entry]))
        )
        context = _context(tmp_path)
        context.config.storage.path = str(tmp_path / "mail.db")
        context.config.logging.file = context.config.logging.console = False
        export_nonebot(context)
        spec = importlib.util.spec_from_file_location(
            "generated_nonebot", tmp_path / "src/nonebot_plugin_mailflow/__init__.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert len(driver.startups) == len(driver.shutdowns) == 1
        await driver.startups[0]()
        service = module.__dict__["_service"]
        try:
            assert isinstance(service, MailFlowService)
            assert service.registry.has(ComponentKind.BOT_EXPORTER, "fake")
            await driver.startups[0]()
            assert module.__dict__["_service"] is service
            service.config.notifiers = [
                NotifierConfig(
                    notifier_id="bot-1",
                    provider="onebot",
                    options={"admins": ["42"]},
                )
            ]
            bot = SimpleNamespace(
                self_id="bot-1", adapter=SimpleNamespace(get_name=lambda: "OneBot V11")
            )
            event = SimpleNamespace(
                get_plaintext=lambda: "/mailflow clean records",
                get_user_id=lambda: "reader",
                group_id="chat-1",
            )
            matcher.send = AsyncMock()
            service.clear_all_records = AsyncMock(return_value=(0, 0))
            await module.__dict__["_handle_mailflow"](bot, event)
            service.clear_all_records.assert_not_awaited()
            assert service.t("permission.denied") in matcher.send.call_args.args[0]
            observed: list[PermissionContext] = []

            async def clear() -> tuple[int, int]:
                observed.append(current_context())
                return 0, 0

            service.clear_all_records.side_effect = clear
            event.get_user_id = lambda: "42"
            await module.__dict__["_handle_mailflow"](bot, event)
            assert observed[0].sender == "42"
            assert observed[0].instance_id == "bot-1" and observed[0].chat_id == "chat-1"
        finally:
            await driver.shutdowns[0]()
        assert module.__dict__["_service"] is None
        assert not service.started


class TestAstrbotExporter:
    async def test_exported_handler_preserves_identity_and_shared_permission(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mailflow_test_support.commands import commands_service

        def command(name: str) -> Any:
            def decorate(method: Any) -> Any:
                return method

            return decorate

        def plain_result(text: str) -> str:
            return text

        event_module = ModuleType("astrbot.api.event")
        event_module.__dict__.update(
            AstrMessageEvent=object,
            filter=SimpleNamespace(command=command),
        )
        star_module = ModuleType("astrbot.api.star")
        star_module.__dict__.update(Context=object, Star=object)
        monkeypatch.setitem(sys.modules, "astrbot", ModuleType("astrbot"))
        monkeypatch.setitem(sys.modules, "astrbot.api", ModuleType("astrbot.api"))
        monkeypatch.setitem(sys.modules, "astrbot.api.event", event_module)
        monkeypatch.setitem(sys.modules, "astrbot.api.star", star_module)
        export_astrbot(_context(tmp_path))
        spec = importlib.util.spec_from_file_location("fake_astrbot_plugin", tmp_path / "main.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        service = commands_service()
        service.config.notifiers = [
            NotifierConfig(
                notifier_id="bot-1",
                provider="telegram",
                options={"admins": ["42"]},
            )
        ]
        module.__dict__.update(_service=service, _router=CommandRouter(service))
        event = SimpleNamespace(
            message_str="/mailflow clean records",
            get_sender_id=lambda: "reader",
            get_group_id=lambda: "chat-1",
            platform_meta=SimpleNamespace(name="telegram", id="bot-1"),
            plain_result=plain_result,
        )
        clear = AsyncMock(return_value=(0, 0))
        service.clear_all_records = clear
        handler = module.__dict__["Main"].mailflow
        replies = [reply async for reply in handler(object(), event)]
        assert service.t("permission.denied") in replies[0]
        clear.assert_not_awaited()
        contexts: list[PermissionContext] = []

        async def remove() -> tuple[int, int]:
            contexts.append(current_context())
            return 0, 0

        clear.side_effect = remove
        event.get_sender_id = lambda: "42"
        assert [reply async for reply in handler(object(), event)]
        assert contexts[0].sender == "42" and contexts[0].provider == "telegram"
        assert contexts[0].instance_id == "bot-1" and contexts[0].chat_id == "chat-1"
