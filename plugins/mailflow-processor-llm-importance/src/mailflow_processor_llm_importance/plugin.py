"""LLM importance processor: maps chat-completions JSON into MailFlow domain.

Prompts with the exact four-level urgency semantics, parses structured JSON
(summary/urgency/reason/reply_required/suggested_reply/action_items/notes),
maps timed action items with a mail_id backlink, tolerates fenced or
prose-wrapped JSON, normalizes urgency case and records which backend/LLM
actually served the request.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from mailflow.config import ProcessorConfig
from mailflow.contracts import LLMRouter, ProcessingContext, ProcessorResult
from mailflow.domain import (
    ActionItem,
    ComponentKind,
    MailAnalysis,
    MailMessage,
    Urgency,
    parse_urgency,
)
from mailflow.plugins import PluginInfo
from mailflow.registry import PluginRegistrar
from pydantic import BaseModel, Field

logger = logging.getLogger("mailflow.processor.llm_importance")

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
    processor_id = "llm-importance"

    def __init__(self, config: ProcessorConfig, router: LLMRouter) -> None:
        self._config = config
        self._router = router
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
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

    async def process(self, mail: MailMessage, context: ProcessingContext) -> ProcessorResult:
        primary = self._config.llm
        if primary is None:
            return ProcessorResult(
                analysis=MailAnalysis(
                    summary=mail.subject,
                    urgency=Urgency.INFO,
                    reason="no llm configured for this processor",
                )
            )
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
        return ProcessorResult(
            analysis=analysis,
            llm_used=completion.llm_id,
            llm_backend=completion.backend,
        )


PLUGIN_INFO = PluginInfo(
    plugin_id="mailflow-processor-llm-importance",
    name="LLM Importance Processor",
    version="0.1.0",
    description="Classifies mail urgency and extracts timed actions via chat completions",
    kinds=[ComponentKind.MAIL_PROCESSOR],
)


class LLMImportancePlugin:
    def mailflow_plugin_info(self) -> PluginInfo:
        return PLUGIN_INFO

    def mailflow_register(self, registrar: PluginRegistrar, config: Any) -> None:
        registrar.add_processor("llm-importance", LLMImportanceProcessor)


plugin = LLMImportancePlugin()

__all__ = [
    "ActionPayload",
    "AnalysisPayload",
    "LLMImportanceProcessor",
    "extract_json",
    "parse_due_at",
    "plugin",
]
