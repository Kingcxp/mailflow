"""Built-in processors: the deterministic rules pre-filter and the LLM
importance classifier, shipped with the core (no plugin install needed).

The LLM processor is the extension point for *processor* plugins: an
:class:`LLMEnhancer` (registered via ``registrar.add_llm_enhancer``) can
append to the system prompt, add extra chat messages, and post-process the
LLM output — bounded, composable customization without reimplementing the
classification itself.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from mailflow.config import ProcessorConfig
from mailflow.contracts import LLMEnhancer, LLMRouter, ProcessingContext, ProcessorResult
from mailflow.domain import (
    ActionItem,
    MailAnalysis,
    MailMessage,
    Urgency,
    parse_urgency,
)

logger = logging.getLogger("mailflow.processor")

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


class RulesProcessor:
    """Deterministic ad/sender pre-filter before any LLM work."""

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


SYSTEM_PROMPT = """You classify email into exactly four importance levels:
- "ad": irrelevant advertising/junk (gray #909399). Do NOT reply to these.
- "info": contains useful information but nothing time-critical (green #67C23A),
  e.g. a lecture notice you may ignore safely.
- "important": needs reading (orange #E6A23C), e.g. a verification code.
- "urgent": must be handled now or at a specific time (red #F56C6C),
  e.g. picking up an ID card, attending an exam, joining an academic meeting.

Rules:
1. Never invent facts that are not in the mail. If a field is unknown use "".
2. "reply_required" is true ONLY when the sender explicitly asks for a
   response or the mail clearly expects one (RSVP, question, confirmation).
3. For every timed obligation (exam, meeting, errand, deadline) produce an
   action item. action_type is one of: "exam", "meeting", "errand", "other".
4. notes for an action item must list practical preparations mentioned in the
   mail (what to bring, what to wear, materials to prepare).
5. Output ONLY a single JSON object, no prose, no markdown fences:
{
  "summary": "one or two sentence summary",
  "urgency": "ad|info|important|urgent",
  "reason": "short reason for the urgency",
  "reply_required": true,
  "suggested_reply": "draft reply text if reply_required else empty",
  "action_items": [
    {
      "summary": "what must be done",
      "action_type": "exam|meeting|errand|other",
      "due_at": "ISO-8601 datetime with timezone offset",
      "due_end": "ISO-8601 datetime or null for point events",
      "notes": "preparations: what to bring/wear/prepare"
    }
  ],
  "notes": "anything else worth remembering"
}
"""

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ActionPayload(BaseModel):
    summary: str
    action_type: str = "other"
    due_at: str
    due_end: str | None = None
    notes: str = ""


class AnalysisPayload(BaseModel):
    summary: str
    urgency: str
    reason: str = ""
    reply_required: bool = False
    suggested_reply: str = ""
    action_items: list[ActionPayload] = Field(default_factory=lambda: [])
    notes: str = ""


def extract_json(text: str) -> dict[str, Any]:
    """Parse JSON from an LLM response, tolerating fences and surrounding prose."""
    fenced = _JSON_FENCE.search(text)
    candidate = fenced.group(1) if fenced else text
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            raise
        parsed = json.loads(candidate[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("llm response is not a JSON object")
    result: dict[str, Any] = {}
    for key, value in cast(dict[str, Any], parsed).items():
        result[str(key)] = value
    return result


def parse_due_at(value: str, timezone: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
    return parsed


class LLMImportanceProcessor:
    """Classifies urgency and extracts timed actions via chat completions.

    ``enhancers`` (list of :class:`LLMEnhancer`) let processor plugins
    extend the prompt and adjust the output without replacing the
    classification logic.
    """

    processor_id = "llm-importance"

    def __init__(
        self,
        config: ProcessorConfig,
        router: LLMRouter,
        enhancers: list[LLMEnhancer] | None = None,
    ) -> None:
        self._config = config
        self._router = router
        self._enhancers = list(enhancers or [])
        self._max_summary_chars = int(config.options.get("max_summary_chars", 600))
        self._max_body_chars = int(config.options.get("max_body_chars", 6000))

    def _build_messages(
        self, mail: MailMessage, context: ProcessingContext
    ) -> list[dict[str, str]]:
        now = context.now or datetime.now()
        body = mail.body_text or mail.body_html or ""
        body = body[: self._max_body_chars]
        user = (
            f"Current time (UTC): {now.isoformat()}\n"
            f"Timezone: {context.timezone}\n"
            f"Mail received: {mail.received_at.isoformat()}\n"
            f"From: {mail.sender.display}\n"
            f"To: {', '.join(r.display for r in mail.recipients)}\n"
            f"Subject: {mail.subject}\n"
            f"Body:\n{body}\n"
        )
        if context.feedback_guidelines:
            user += (
                "\nUser feedback on previously received mail (treat as strong "
                "priorities; e.g. mark matching mail lower importance):\n"
                f"{context.feedback_guidelines}\n"
            )
        system_prompt = SYSTEM_PROMPT
        for enhancer in self._enhancers:
            system_prompt = enhancer.system_prompt(system_prompt)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user},
        ]
        for enhancer in self._enhancers:
            messages.extend(enhancer.extra_messages(mail, context))
        return messages

    async def process(self, mail: MailMessage, context: ProcessingContext) -> ProcessorResult:
        primary = self._config.llm
        if primary is None:
            # Nothing to classify without an LLM: return no overlay so the
            # deterministic rules result (and later processors) survive.
            return ProcessorResult()
        messages = self._build_messages(mail, context)
        completion = await self._router.chat(
            messages,
            primary=primary,
            fallback=list(self._config.fallback_llms),
            options={"temperature": 0.2},
        )
        payload = AnalysisPayload.model_validate(extract_json(completion.text))
        urgency = parse_urgency(payload.urgency)
        action_items = [
            ActionItem(
                item_id=uuid.uuid4().hex[:16],
                mail_id=mail.message_id,
                summary=item.summary[:200],
                action_type=item.action_type,
                due_at=parse_due_at(item.due_at, context.timezone),
                due_end=(parse_due_at(item.due_end, context.timezone) if item.due_end else None),
                notes=item.notes[:300],
            )
            for item in payload.action_items
        ]
        analysis = MailAnalysis(
            summary=payload.summary[: self._max_summary_chars] or mail.subject,
            urgency=urgency,
            reason=payload.reason[:300],
            reply_required=payload.reply_required,
            suggested_reply=payload.suggested_reply[:2000],
            action_items=action_items,
            notes=payload.notes[:500],
            backend=completion.backend,
        )
        for enhancer in self._enhancers:
            adjusted = enhancer.post_process(analysis, mail, context)
            if adjusted is not None:
                analysis = adjusted
        return ProcessorResult(
            analysis=analysis,
            llm_used=completion.llm_id,
            llm_backend=completion.backend,
        )


def register_builtin_processors(registry: Any) -> None:
    """Register the built-in ``rules`` and ``llm-importance`` processors.

    A plugin registering the same component id wins (the built-in is
    skipped), so third-party implementations can replace the defaults.
    """
    from mailflow.domain import ComponentKind

    for component_id, factory in (
        ("rules", RulesProcessor),
        ("llm-importance", LLMImportanceProcessor),
    ):
        if registry.has(ComponentKind.MAIL_PROCESSOR, component_id):
            continue
        registry.register(
            ComponentKind.MAIL_PROCESSOR,
            component_id,
            "mailflow-core",
            factory,
        )


__all__ = [
    "SYSTEM_PROMPT",
    "ActionPayload",
    "AnalysisPayload",
    "LLMImportanceProcessor",
    "RulesProcessor",
    "extract_json",
    "parse_due_at",
    "register_builtin_processors",
]
