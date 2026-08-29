"""MailFlow composition root.

Registers the official built-in plugin set through static imports (frozen
builds do not depend on entry-point metadata) and optionally discovers
external entry-point plugins on top.
"""

from __future__ import annotations

from typing import Any

from mailflow.config import MailFlowConfig
from mailflow.plugins import PluginManager
from mailflow_export_astrbot.plugin import plugin as astrbot_export_plugin
from mailflow_export_nonebot.plugin import plugin as nonebot_export_plugin
from mailflow_llm_anthropic.plugin import plugin as anthropic_plugin
from mailflow_llm_google_generative_ai.plugin import plugin as google_gemini_plugin
from mailflow_llm_google_vertex.plugin import plugin as google_vertex_plugin
from mailflow_llm_openai_compatible.plugin import plugin as openai_plugin
from mailflow_mail_fake.plugin import plugin as fake_plugin
from mailflow_mail_imap.plugin import plugin as imap_plugin
from mailflow_notify_console.plugin import plugin as notify_plugin
from mailflow_notify_onebot.plugin import plugin as onebot_plugin
from mailflow_notify_openclaw_weixin.plugin import plugin as openclaw_weixin_plugin
from mailflow_notify_openwechat.plugin import plugin as openwechat_plugin
from mailflow_notify_wechaty.plugin import plugin as wechaty_plugin
from mailflow_storage_sqlite.plugin import plugin as storage_plugin

BUNDLED_PLUGINS: tuple[Any, ...] = (
    fake_plugin,
    imap_plugin,
    storage_plugin,
    openai_plugin,
    anthropic_plugin,
    google_gemini_plugin,
    google_vertex_plugin,
    notify_plugin,
    onebot_plugin,
    wechaty_plugin,
    openwechat_plugin,
    openclaw_weixin_plugin,
    nonebot_export_plugin,
    astrbot_export_plugin,
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
