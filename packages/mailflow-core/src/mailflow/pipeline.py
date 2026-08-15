"""Ordered processor pipeline with per-processor timeout, retries and policy.

Owns execution semantics that pluggy deliberately does not: sorting by
priority, retry limits, timeouts, ``continue``/``stop`` failure policy, the
per-processor ``ProcessorNote`` trail, and the final fallback-summary
guarantee (a mail is never stored without a summary).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from mailflow.config import ProcessorConfig
from mailflow.contracts import (
    LLMRouter,
    MailProcessor,
    ProcessingContext,
    ProcessorDecision,
    ProcessorResult,
)
from mailflow.domain import MailAnalysis, MailMessage, ProcessorNote, Urgency, utcnow

logger = logging.getLogger("mailflow.pipeline")


@dataclass(frozen=True)
class ProcessorBinding:
    """One configured processor instance bound to its plugin and LLM."""

    processor_id: str
    plugin_id: str
    processor: MailProcessor
    priority: int = 100
    llm: str | None = None
    fallback_llms: list[str] = field(default_factory=lambda: [])
    failure_policy: str = "continue"
    retries: int = 1
    timeout_seconds: float = 30.0
    options: dict[str, Any] = field(default_factory=lambda: {})


def merge_analysis(base: MailAnalysis, overlay: MailAnalysis) -> MailAnalysis:
    """Merge a processor's overlay into the accumulated analysis.

    Overlay wins per field when it carries a value; urgency and the reply flag
    always come from the overlay (a processor that produced an analysis
    decided those). Field-order is the pipeline order, so later processors
    override earlier ones.
    """
    merged = base.model_copy(deep=True)
    if overlay.summary:
        merged.summary = overlay.summary
    merged.urgency = overlay.urgency
    merged.reply_required = overlay.reply_required
    if overlay.reason:
        merged.reason = overlay.reason
    if overlay.suggested_reply:
        merged.suggested_reply = overlay.suggested_reply
    if overlay.action_items:
        merged.action_items = overlay.action_items
    if overlay.notes:
        merged.notes = overlay.notes
    if overlay.backend:
        merged.backend = overlay.backend
    return merged


class PipelineEngine:
    """Runs the sorted binding chain for one mail and returns the analysis."""

    def __init__(self, bindings: list[ProcessorBinding], router: LLMRouter | None = None) -> None:
        self._bindings = sorted(bindings, key=lambda b: (b.priority, b.processor_id))
        self._router = router

    @property
    def bindings(self) -> list[ProcessorBinding]:
        return list(self._bindings)

    def _sanitize(self, message: str) -> str:
        # Belt-and-braces: strip anything that looks like a credential from
        # persisted notes. Backend plugins are expected to sanitize already.
        for fragment in ("api_key=", "access_token=", "Authorization: Bearer "):
            if fragment in message:
                message = f"{message.split(fragment, 1)[0]}[redacted]"
        return message[:500]

    async def _run_with_retries(
        self, binding: ProcessorBinding, mail: MailMessage, context: ProcessingContext
    ) -> ProcessorResult:
        last_error: Exception | None = None
        for attempt in range(binding.retries + 1):
            try:
                return await asyncio.wait_for(
                    binding.processor.process(mail, context),
                    timeout=binding.timeout_seconds,
                )
            except Exception as exc:
                last_error = exc
                if attempt >= binding.retries:
                    break
                logger.debug(
                    "processor %r attempt %d/%d failed: %s",
                    binding.processor_id,
                    attempt + 1,
                    binding.retries + 1,
                    exc,
                )
        assert last_error is not None
        raise last_error

    async def process(
        self,
        mail: MailMessage,
        account_id: str,
        *,
        timezone: str = "UTC",
        now: datetime | None = None,
    ) -> tuple[MailAnalysis, list[ProcessorNote], str, str]:
        """Returns ``(analysis, notes, llm_used, llm_backend)``."""
        accumulated = MailAnalysis(summary="", urgency=Urgency.INFO)
        notes: list[ProcessorNote] = []
        llm_used = ""
        llm_backend = ""
        stop_requested = False

        for binding in self._bindings:
            if stop_requested:
                break
            started_at = now or utcnow()
            context = ProcessingContext(
                account_id=account_id,
                timezone=timezone,
                options=binding.options,
                now=started_at,
            )
            try:
                result = await self._run_with_retries(binding, mail, context)
            except Exception as exc:
                finished_at = now or utcnow()
                message = f"failed: {self._sanitize(str(exc))}"
                notes.append(
                    ProcessorNote(
                        processor_id=binding.processor_id,
                        plugin_id=binding.plugin_id,
                        status="failed",
                        message=message,
                        started_at=started_at,
                        finished_at=finished_at,
                    )
                )
                logger.warning(
                    "processor %r failed for mail %r: %s",
                    binding.processor_id,
                    mail.message_id,
                    exc,
                )
                if binding.failure_policy == "stop":
                    logger.error(
                        "failure_policy=stop; halting pipeline after %r", binding.processor_id
                    )
                    stop_requested = True
                continue

            finished_at = now or utcnow()
            if result.analysis is not None:
                accumulated = merge_analysis(accumulated, result.analysis)
            if result.llm_used:
                llm_used = result.llm_used
            if result.llm_backend:
                llm_backend = result.llm_backend
            message = "; ".join(result.notes) if result.notes else "ok"
            notes.append(
                ProcessorNote(
                    processor_id=binding.processor_id,
                    plugin_id=binding.plugin_id,
                    status="success",
                    message=message,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            )
            if result.decision == ProcessorDecision.STOP:
                logger.debug("processor %r requested pipeline stop", binding.processor_id)
                stop_requested = True

        if not accumulated.summary:
            accumulated.summary = mail.subject or "(no subject)"
            notes.append(
                ProcessorNote(
                    processor_id="pipeline",
                    plugin_id="mailflow-core",
                    status="success",
                    message="fallback summary used (no processor produced one)",
                    started_at=now or utcnow(),
                    finished_at=now or utcnow(),
                )
            )
        if not accumulated.backend:
            accumulated.backend = llm_backend
        return accumulated, notes, llm_used, llm_backend


def build_bindings(
    configs: list[ProcessorConfig],
    processors: dict[str, MailProcessor],
    plugin_of: dict[str, str],
) -> list[ProcessorBinding]:
    """Build sorted bindings from config, resolved processor instances and ownership."""
    bindings: list[ProcessorBinding] = []
    for config in configs:
        if not config.enabled:
            continue
        processor = processors.get(config.processor_id)
        if processor is None:
            raise ValueError(f"processor {config.processor_id!r} not registered by any plugin")
        bindings.append(
            ProcessorBinding(
                processor_id=config.processor_id,
                plugin_id=plugin_of.get(config.processor_id, ""),
                priority=config.priority,
                processor=processor,
                llm=config.llm,
                fallback_llms=list(config.fallback_llms),
                failure_policy=config.failure_policy,
                retries=config.retries,
                timeout_seconds=config.timeout_seconds,
                options=dict(config.options),
            )
        )
    return bindings


__all__ = [
    "PipelineEngine",
    "ProcessorBinding",
    "build_bindings",
    "merge_analysis",
]
