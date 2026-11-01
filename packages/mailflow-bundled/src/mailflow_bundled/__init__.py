"""Shared composition root: bundled plugins first, then installed plugins.

Explicit module names also work in frozen builds. Imports are isolated so a
missing dependency costs its plugin, not the whole host.
"""

from __future__ import annotations

from functools import partial
from importlib import import_module
from typing import Any

from mailflow.config import MailFlowConfig
from mailflow.plugins import PluginManager

BUNDLED_PLUGINS: tuple[str, ...] = (
    "mailflow_mail_fake",
    "mailflow_mail_imap",
    "mailflow_storage_sqlite",
    "mailflow_llm_openai_compatible",
    "mailflow_llm_anthropic",
    "mailflow_llm_google_generative_ai",
    "mailflow_llm_google_vertex",
    "mailflow_notify_console",
    "mailflow_notify_onebot",
    "mailflow_notify_openwechat",
    "mailflow_notify_telegram",
    "mailflow_notify_wechatpadpro",
    "mailflow_notify_whatsapp",
    "mailflow_export_nonebot",
    "mailflow_export_astrbot",
)


def _import_plugin(module: str) -> Any:
    return import_module(f"{module}.plugin").plugin


def create_plugin_manager(
    config: MailFlowConfig | None = None,
    *,
    discover_external: bool = True,
) -> PluginManager:
    """The standard manager: bundled set first, optional external discovery."""
    manager = PluginManager(config)
    for module in BUNDLED_PLUGINS:
        manager.load(
            module.replace("_", "-"),
            partial(_import_plugin, module),
            bundled=True,
        )
    if discover_external:
        manager.discover()
    return manager


__all__ = ["BUNDLED_PLUGINS", "create_plugin_manager"]
