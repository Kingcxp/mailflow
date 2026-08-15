"""Deterministic rules processor: cheap signals before any LLM work.

- advertising keywords (word-boundary matched, configurable) -> gray AD
- exact important-sender addresses (configurable) -> orange IMPORTANT
No LLM is ever consulted here; when nothing matches the processor is a no-op
and later processors decide.
"""

from __future__ import annotations

import re
from typing import Any

from mailflow.config import ProcessorConfig
from mailflow.contracts import LLMRouter, ProcessingContext, ProcessorResult
from mailflow.domain import MailAnalysis, MailMessage, Urgency
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar

_DEFAULT_KEYWORDS = (
    "unsubscribe",
    "promotion",
    "sale",
    "discount",
    "advertisement",
    "limited offer",
    "act now",
    "click here",
)


class RuleProcessor:
    processor_id = "rules"

    def __init__(self, config: ProcessorConfig, router: LLMRouter | None = None) -> None:
        self._keywords: list[str] = [
            str(kw).lower() for kw in config.options.get("advertising_keywords", _DEFAULT_KEYWORDS)
        ]
        self._important_senders: list[str] = [
            str(addr).lower() for addr in config.options.get("important_senders", [])
        ]

    def _is_advertisement(self, haystack: str) -> bool:
        return any(re.search(rf"\b{re.escape(keyword)}\b", haystack) for keyword in self._keywords)

    def _is_important_sender(self, sender_address: str) -> bool:
        normalized = sender_address.lower()
        return any(normalized == important for important in self._important_senders)

    async def process(self, mail: MailMessage, context: ProcessingContext) -> ProcessorResult:
        haystack = f"{mail.subject}\n{mail.body_text}".lower()
        if self._is_advertisement(haystack):
            return ProcessorResult(
                analysis=MailAnalysis(
                    summary="Advertisement detected by rules",
                    urgency=Urgency.AD,
                    reason="matches advertising keywords",
                    backend="",
                )
            )
        if self._is_important_sender(mail.sender.address):
            return ProcessorResult(
                analysis=MailAnalysis(
                    summary=mail.subject,
                    urgency=Urgency.IMPORTANT,
                    reason="sender is on the important-senders list",
                    backend="",
                )
            )
        return ProcessorResult()


PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-processor-rules",
    name="Rules Processor",
    version="0.1.0",
    description="Deterministic ad/sender pre-filter before any LLM work",
)


class RulesPlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        registrar.add_processor("rules", RuleProcessor)


plugin = RulesPlugin()

__all__ = ["RuleProcessor", "RulesPlugin", "plugin"]
