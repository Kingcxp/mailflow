"""NoneBot exporter plugin: registers the ``nonebot`` framework exporter."""

from __future__ import annotations

from typing import Any

from mailflow.domain import ComponentKind
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

from mailflow_export_nonebot.exporter import export_nonebot

PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-export-nonebot",
    name="NoneBot Exporter",
    version="0.1.0",
    description="Exports a configured MailFlow instance as a NoneBot2 plugin",
    kinds=[ComponentKind.BOT_EXPORTER],
)


class ExportPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        registrar.add_bot_exporter("nonebot", export_nonebot)


plugin = ExportPlugin()

__all__ = ["PLUGIN_INFO", "ExportPlugin", "plugin"]
