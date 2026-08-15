"""Console notifier: surfaces already-computed mail analyses through the log
system (terminal + file sinks), so no additional transport is needed and the
message content stays visible in the rich log output.
"""

from __future__ import annotations

import logging
from typing import Any

from mailflow.domain import ComponentKind, MailRecord
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

logger = logging.getLogger("mailflow.notification.console")

PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-notify-console",
    name="Console Notifier",
    version="0.1.0",
    description="Delivers processed mail alerts to the MailFlow logs",
    kinds=[ComponentKind.NOTIFIER],
)


class ConsoleNotifier:
    async def notify(self, record: MailRecord) -> None:
        urgency = record.effective_urgency
        summary = record.summary
        logger.warning(
            "NOTIFY [%s] %s — %s (%s)",
            urgency.value,
            record.mail.subject,
            summary,
            record.mail.sender.address,
        )


class NotifyPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        registrar.add_notifier("console", lambda notifier_config: ConsoleNotifier())


plugin = NotifyPlugin()

__all__ = ["ConsoleNotifier", "NotifyPlugin", "plugin"]
