"""AstrBot exporter plugin: registers the ``astrbot`` framework exporter."""

from __future__ import annotations

from typing import Any

from mailflow.domain import ComponentKind
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

from mailflow_export_astrbot.exporter import export_astrbot

PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-export-astrbot",
    name="AstrBot Exporter",
    version="0.1.0",
    description="Exports a configured MailFlow instance as an AstrBot plugin",
    kinds=[ComponentKind.BOT_EXPORTER],
)


class ExportPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        registrar.add_bot_exporter("astrbot", export_astrbot)


plugin = ExportPlugin()

__all__ = ["PLUGIN_INFO", "ExportPlugin", "plugin"]
