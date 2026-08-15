"""MailFlow composition root.

Registers the official built-in plugin set through static imports (frozen
builds do not depend on entry-point metadata) and optionally discovers
external entry-point plugins on top.
"""

from __future__ import annotations

from typing import Any

from mailflow.config import MailFlowConfig
from mailflow.plugins import PluginManager
from mailflow_llm_openai_compatible.plugin import plugin as openai_plugin
from mailflow_mail_fake.plugin import plugin as fake_plugin
from mailflow_notify_console.plugin import plugin as notify_plugin
from mailflow_processor_llm_importance.plugin import plugin as llm_processor_plugin
from mailflow_processor_rules.plugin import plugin as rules_plugin
from mailflow_storage_sqlite.plugin import plugin as storage_plugin

BUNDLED_PLUGINS: tuple[Any, ...] = (
    fake_plugin,
    storage_plugin,
    openai_plugin,
    rules_plugin,
    llm_processor_plugin,
    notify_plugin,
)


def create_plugin_manager(
    config: MailFlowConfig | None = None,
    *,
    discover_external: bool = True,
) -> PluginManager:
    """The standard manager: bundled set first, optional external discovery."""
    manager = PluginManager(config)
    for plugin in BUNDLED_PLUGINS:
        manager.register(plugin)
    if discover_external:
        manager.discover()
    return manager


__all__ = ["BUNDLED_PLUGINS", "create_plugin_manager"]
