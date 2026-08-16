"""Bot-framework export: registry wiring, the shared export entry point and
the built-in NoneBot/AstrBot exporters (file generation + config round-trip)."""

from __future__ import annotations

from pathlib import Path

import pytest
from mailflow.bot_export import (
    BotExportContext,
    BotExportResult,
    available_frameworks,
    export_bot_plugin,
)
from mailflow.config import MailFlowConfig, load_config
from mailflow.domain import ComponentKind
from mailflow.plugins import PluginInfo
from mailflow.registry import ComponentRegistry, PluginRegistrar
from mailflow_export_astrbot.exporter import export_astrbot
from mailflow_export_nonebot.exporter import export_nonebot


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
# Registry wiring
# ---------------------------------------------------------------------------


class TestBotExporterRegistry:
    def test_registration_and_lookup(self) -> None:
        registry = _registry_with(FakeExporterPlugin())
        assert available_frameworks(registry) == ["fake"]
        factory = registry.bot_exporter_factory("fake")
        assert callable(factory)

    def test_ownership_is_stamped(self) -> None:
        registry = _registry_with(FakeExporterPlugin())
        assert registry.plugin_for("fake") == "mailflow-export-fake"
        snapshot = registry.snapshots()
        assert (snapshot[0].kind, snapshot[0].component_id) == (
            ComponentKind.BOT_EXPORTER,
            "fake",
        )

    def test_unknown_framework_raises_key_error(self) -> None:
        registry = _registry_with(FakeExporterPlugin())
        with pytest.raises(KeyError):
            registry.bot_exporter_factory("missing")

    def test_duplicate_framework_rejected(self) -> None:
        registry = ComponentRegistry()
        registrar = PluginRegistrar(registry, MailFlowConfig(), "p1")
        registrar.add_bot_exporter("dup", lambda ctx: ctx)  # type: ignore[return-value]
        registrar2 = PluginRegistrar(registry, MailFlowConfig(), "p2")
        with pytest.raises(ValueError):
            registrar2.add_bot_exporter("dup", lambda ctx: ctx)  # type: ignore[return-value]


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
            output_dir=tmp_path,
            plugin_ids=["mailflow-a", "mailflow-b"],
            version="1.2.3",
            language="zh-CN",
        )
        assert result.framework == "fake"
        assert result.plugin_name == "fake_plugin"
        assert result.created == ["README.md"]
        assert (tmp_path / "README.md").is_file()
        assert "plugins=['mailflow-a', 'mailflow-b'] version=1.2.3" in result.notes

    def test_unknown_framework_raises_key_error(self, tmp_path: Path) -> None:
        registry = _registry_with(FakeExporterPlugin())
        with pytest.raises(KeyError):
            export_bot_plugin(registry, MailFlowConfig(), framework="nope", output_dir=tmp_path)

    def test_output_dir_is_created(self, tmp_path: Path) -> None:
        registry = _registry_with(FakeExporterPlugin())
        nested = tmp_path / "a" / "b"
        result = export_bot_plugin(registry, MailFlowConfig(), framework="fake", output_dir=nested)
        assert (nested / "README.md").is_file()
        assert result.created == ["README.md"]


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
    def test_generates_plugin_package(self, tmp_path: Path) -> None:
        result = export_nonebot(_context(tmp_path))
        assert result.framework == "nonebot"
        assert result.plugin_name == "nonebot-plugin-mailflow"
        expected = {
            "pyproject.toml",
            "README.md",
            "src/nonebot_plugin_mailflow/__init__.py",
            "src/nonebot_plugin_mailflow/config.toml",
        }
        assert set(result.created) == expected
        for relative in result.created:
            assert (tmp_path / relative).is_file()

    def test_pyproject_declares_framework_and_plugins(self, tmp_path: Path) -> None:
        export_nonebot(_context(tmp_path))
        pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
        assert 'name = "nonebot-plugin-mailflow"' in pyproject
        assert 'version = "9.9.9"' in pyproject
        assert "nonebot2>=2.0.0" in pyproject
        assert "mailflow-core" in pyproject
        assert "mailflow-bundled" in pyproject
        assert "mailflow-storage-sqlite" in pyproject
        assert "mailflow-notify-console" in pyproject

    def test_plugin_init_registers_driver_hooks(self, tmp_path: Path) -> None:
        export_nonebot(_context(tmp_path))
        init = (tmp_path / "src/nonebot_plugin_mailflow/__init__.py").read_text(encoding="utf-8")
        assert "driver.on_startup" in init
        assert "driver.on_shutdown" in init
        assert "start_service(config, plugin_manager=create_plugin_manager(config))" in init
        assert "config.toml" in init

    def test_embedded_config_round_trips(self, tmp_path: Path) -> None:
        export_nonebot(_context(tmp_path))
        config = load_config(tmp_path / "src/nonebot_plugin_mailflow/config.toml")
        assert isinstance(config, MailFlowConfig)


class TestAstrbotExporter:
    def test_generates_plugin_folder(self, tmp_path: Path) -> None:
        result = export_astrbot(_context(tmp_path))
        assert result.framework == "astrbot"
        assert result.plugin_name == "astrbot_plugin_mailflow"
        expected = {"metadata.yaml", "main.py", "README.md", "requirements.txt", "config.toml"}
        assert set(result.created) == expected
        for relative in result.created:
            assert (tmp_path / relative).is_file()

    def test_main_implements_star_lifecycle(self, tmp_path: Path) -> None:
        export_astrbot(_context(tmp_path))
        main = (tmp_path / "main.py").read_text(encoding="utf-8")
        assert "from astrbot.api.star import Context, Star" in main
        assert "class Main(Star)" in main
        assert "async def initialize" in main
        assert "async def terminate" in main

    def test_metadata_and_requirements(self, tmp_path: Path) -> None:
        export_astrbot(_context(tmp_path))
        metadata = (tmp_path / "metadata.yaml").read_text(encoding="utf-8")
        assert "name: astrbot_plugin_mailflow" in metadata
        assert "version: 9.9.9" in metadata
        requirements = (tmp_path / "requirements.txt").read_text(encoding="utf-8").splitlines()
        assert "mailflow-core" in requirements
        assert "mailflow-bundled" in requirements
        assert "mailflow-storage-sqlite" in requirements
        assert "mailflow-notify-console" in requirements

    def test_embedded_config_round_trips(self, tmp_path: Path) -> None:
        export_astrbot(_context(tmp_path))
        config = load_config(tmp_path / "config.toml")
        assert isinstance(config, MailFlowConfig)
