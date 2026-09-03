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

from pydantic import BaseModel, Field, ValidationError

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


def _plain_body(mail: MailMessage) -> str:
    """Best-effort text body; HTML-only mails are tag-stripped so keyword
    scanning and the LLM prompt both see usable content."""
    if mail.body_text:
        return mail.body_text
    if mail.body_html:
        return re.sub(r"<[^>]+>", " ", mail.body_html)
    return ""


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
        haystack = f"{mail.subject}\n{_plain_body(mail)}".lower()
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


SYSTEM_PROMPT = """You triage a university student's email into exactly four
importance levels. Judge from the perspective of the recipient: what would a
busy student actually need to act on?

- "urgent" (red #F56C6C): the recipient MUST physically or digitally act at a
  specific date/time within the next few days — pick up a document (student
  card, certificate), attend an exam/meeting/defense at a stated time, complete
  registration/payment before a deadline, submit paperwork by a date. A due
  date/time is present or clearly implied.
- "important" (orange #E6A23C): needs timely attention but is NOT a physical
  appointment — verification codes, one-time passwords, action-required online
  steps (pay a fee online, confirm enrollment), official notices the recipient
  must read and respond to this week.
- "info" (green #67C23A): optional or FYI content — academic lectures/seminars
  the recipient MAY attend, club activities, general announcements, grade
  postings, newsletters.
- "ad" (gray #909399): marketing, promotions, routine system/account login
  reminders, security-notice boilerplate, password-expiry nudges, delivery
  status updates, and any bulk mail.

Calibration rules:
1. When in doubt between urgent and important, choose important. Urgent is
   reserved for concrete scheduled obligations with a date/time.
2. Login reminders, "your account was accessed", password-expiry notices and
   similar routine system mails are ALWAYS "ad", never important/urgent.
3. Lectures and seminars without mandatory attendance are "info", even with a
   date. Only mark urgent/important if attendance is required for THIS
   recipient (their name, their session, compulsory for their program).
4. Never invent facts not in the mail. Unknown fields use "".
5. reply_required=true ONLY when the sender explicitly expects an answer.
6. Every timed obligation classified urgent/important MUST yield exactly one
   action item with due_at parsed from the mail; action_type ∈
   {"exam","meeting","errand","other"}; notes list practical preparations.
   Category definitions: "exam" = tests/exams/quizzes; "meeting" = scheduled
   meetings, calls, defenses, interviews; "errand" = physical errands and
   deadlines requiring an action (pickups, payments, registrations,
   appointments, submission deadlines); "other" ONLY when none of the three
   fit — always prefer the closest specific category.
7. reason MUST agree with urgency. Never write a reason describing something
   the recipient must act on, read, respond to or track this week and then
   classify it "info". If the mail asks for action, has a deadline, needs a
   response, or the reason says it matters to the recipient, pick
   important (or urgent when a concrete date/time is set). "info" reasons
   must be genuinely optional/FYI (seminar you MAY attend, general notice).
   A reason like "action required" with urgency "info" is a contradiction:
   re-check and raise the urgency.
8. Output ONLY a single JSON object, no prose, no markdown fences:
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


def _as_str(value: Any) -> str:
    """LLMs emit null/numbers/booleans where the schema wants strings."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "是"}
    return bool(value)


def _coerce_analysis_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a model's JSON to the AnalysisPayload schema.

    Strict pydantic validation turned single-field slips (a null summary,
    "reply_required": "true" as a string, a non-list action_items) into a
    failed analysis for the whole mail; every field is now coerced and
    malformed action items are dropped individually.
    """
    coerced: dict[str, Any] = dict(raw)
    for key in ("summary", "urgency", "reason", "suggested_reply", "notes"):
        if key in coerced:
            coerced[key] = _as_str(coerced[key])
    if "reply_required" in coerced:
        coerced["reply_required"] = _as_bool(coerced["reply_required"])
    raw_items: Any = coerced.get("action_items")
    # raw_items comes from model JSON output — genuinely untyped until the
    # pydantic validation below; the explicit list[Any] is the boundary
    items: list[Any] = raw_items if isinstance(raw_items, list) else []  # pyright: ignore[reportUnknownVariableType]
    cleaned: list[dict[str, Any]] = []
    for unknown_item in items:
        if not isinstance(unknown_item, dict):
            continue
        entry = dict(cast(dict[str, Any], unknown_item))
        for key in ("summary", "action_type", "notes"):
            if key in entry:
                entry[key] = _as_str(entry[key])
        for key in ("due_at", "due_end"):
            if key in entry:
                value = entry[key]
                entry[key] = "" if value is None else _as_str(value)
        # models routinely omit the action item's summary while filling
        # notes ("填写提名表格…"); without a fallback one missing field
        # would scrap the whole analysis. Derive a readable summary from
        # the notes or the action type so the item survives validation.
        if not entry.get("summary"):
            entry["summary"] = (
                entry.get("notes")
                or f"[{entry.get('action_type') or 'other'}] {entry.get('due_at') or ''}"
            ).strip()
        cleaned.append(entry)
    coerced["action_items"] = cleaned
    return coerced


class ActionPayload(BaseModel):
    summary: str
    action_type: str = "other"
    due_at: str
    due_end: str | None = None
    notes: str = ""


class AnalysisPayload(BaseModel):
    summary: str = ""
    urgency: str = "info"
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
        body = _plain_body(mail)
        if len(body) > self._max_body_chars:
            # the config knob exists but was never applied: oversized
            # bodies (long HTML mails, emoji-heavy newsletters) push the
            # request past the model's context limit and the gateway
            # answers 400 — non-retryable, so the mail can never be
            # analysed. Truncate here so the request always fits.
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
        language = str(self._config.options.get("language") or "").strip()
        if language:
            user += (
                "\nWrite the summary, reason, suggested reply, action-item "
                f"summaries and notes in the following language: {language}.\n"
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
        # reasoning models (DeepSeek-R1 style) prepend <think>…</think>; strip
        # it so reasoning never leaks into stored summaries or JSON extraction
        clean_text = re.sub(
            r"<think>.*?</think>\s*", "", completion.text, flags=re.DOTALL | re.IGNORECASE
        )
        raw_payload = extract_json(clean_text)
        try:
            payload = AnalysisPayload.model_validate(_coerce_analysis_payload(raw_payload))
        except ValidationError:
            # surface what the model actually returned: a silent fallback
            # summary makes rate-limit-style failures indistinguishable
            # from prompt problems
            logger.warning(
                "llm-importance: unparseable payload for %r: %.400s",
                mail.message_id,
                clean_text,
            )
            raise
        action_items: list[ActionItem] = []
        for position, item in enumerate(payload.action_items):
            try:
                action_items.append(
                    ActionItem(
                        item_id=uuid.uuid4().hex[:16],
                        mail_id=mail.message_id,
                        summary=item.summary[:200],
                        action_type=item.action_type,
                        due_at=parse_due_at(item.due_at, context.timezone),
                        due_end=(
                            parse_due_at(item.due_end, context.timezone) if item.due_end else None
                        ),
                        notes=item.notes[:300],
                    )
                )
            except (ValueError, TypeError) as exc:
                # one malformed item must not scrap the whole analysis
                logger.warning(
                    "dropping malformed action item %d from %r: %s", position, mail.message_id, exc
                )
        urgency = parse_urgency(payload.urgency)
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
