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
from mailflow.contracts import (
    LLMEnhancer,
    LLMRouter,
    ProcessingContext,
    ProcessorResult,
)
from mailflow.domain import (
    ActionItem,
    MailAnalysis,
    MailMessage,
    Urgency,
    parse_urgency,
)

logger = logging.getLogger("mailflow.processor")

_STRONG_KEYWORDS = (
    "promotion",
    "sale",
    "discount",
    "advertisement",
    "limited offer",
    "act now",
)
_WEAK_KEYWORDS = (
    # mailing-list boilerplate: official notices carry these in their footer,
    # so one hit means nothing on its own
    "unsubscribe",
    "click here",
)
_DEFAULT_KEYWORDS = (*_STRONG_KEYWORDS, *_WEAK_KEYWORDS)


def _plain_body(mail: MailMessage) -> str:
    """Best-effort text body; HTML-only mails are tag-stripped so keyword
    scanning and the LLM prompt both see usable content."""
    if mail.body_text:
        return mail.body_text
    if mail.body_html:
        return re.sub(r"<[^>]+>", " ", mail.body_html)
    return ""


def _prompt_guidelines(text: str, *, limit: int = 20) -> str:
    """Distinct feedback notes, newest last, capped for the prompt.

    Stored guidelines can already contain the same sentence many times over
    (an earlier build appended repeats without checking), and a wall of
    identical lines reads to the model as an absolute rule — one user's
    repeated "this is an ad" then reclassified unrelated mail. Collapsing
    repeats here fixes existing stores, not just future ones.
    """
    seen: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped in seen:
            continue
        seen.append(stripped)
    return "\n".join(seen[-limit:])


class RulesProcessor:
    """Cheap deterministic hints *before* the LLM, never a verdict.

    The old keyword rule returned ``STOP``/``ad`` on a single hit, and because
    every bulk mailing — university notices, lecture invitations, newsletters —
    carries "unsubscribe" or "click here" in its footer, that quietly removed
    exactly the mail the user cares about from analysis. A hit is now only a
    hint: it contributes an ``ad`` overlay (the value that survives if the LLM
    fails) and the chain continues, so the model can and does overrule it.
    """

    processor_id = "rules"

    def __init__(self, config: ProcessorConfig, router: LLMRouter | None = None) -> None:
        # a keyword list configured by the user is taken at face value
        configured = config.options.get("advertising_keywords")
        self._strong_keywords: list[str] = [
            str(kw).lower() for kw in (configured if configured else _STRONG_KEYWORDS)
        ]
        self._weak_keywords: list[str] = [] if configured else [kw.lower() for kw in _WEAK_KEYWORDS]
        self._important_senders: list[str] = [
            str(addr).lower() for addr in config.options.get("important_senders", [])
        ]

    def _hits(self, haystack: str, keywords: list[str]) -> list[str]:
        return [
            keyword for keyword in keywords if re.search(rf"\b{re.escape(keyword)}\b", haystack)
        ]

    def _promotional_hits(self, haystack: str) -> list[str]:
        """Keywords that justify a promotional *hint*.

        One strong word is enough; footer boilerplate needs two independent
        hits, which is what separates a promotional mail from an official
        notice that merely offers an unsubscribe link.
        """
        strong = self._hits(haystack, self._strong_keywords)
        if strong:
            return strong
        weak = self._hits(haystack, self._weak_keywords)
        return weak if len(weak) >= 2 else []

    def _is_important_sender(self, sender_address: str) -> bool:
        normalized = sender_address.lower()
        return any(normalized == important for important in self._important_senders)

    async def process(self, mail: MailMessage, context: ProcessingContext) -> ProcessorResult:
        if self._is_important_sender(mail.sender.address):
            # a user-listed sender is at least important, but the LLM may raise
            # it to urgent, so this hint continues the chain too
            return ProcessorResult(
                analysis=MailAnalysis(
                    summary="",
                    urgency=Urgency.IMPORTANT,
                    reason="sender is on the important-senders list",
                    backend="",
                )
            )
        haystack = f"{mail.subject}\n{_plain_body(mail)}".lower()
        hits = self._promotional_hits(haystack)
        if hits:
            return ProcessorResult(
                analysis=MailAnalysis(
                    summary="",
                    urgency=Urgency.AD,
                    reason=f"promotional wording detected ({', '.join(sorted(set(hits))[:3])})",
                    backend="",
                )
            )
        return ProcessorResult()


SYSTEM_PROMPT = """You triage a university student's email into exactly four
importance levels, judged by what the recipient must DO.

- "urgent" (red #F56C6C): act at a stated date/time in the next few days — attend
  an exam/meeting/defense, pick up a document, pay or submit by a deadline, or a
  change to the recipient's own schedule (class moved, exam rescheduled:
  调课/改期/延期/缓考).
- "important" (orange #E6A23C): the recipient must act, reply or read something
  they are responsible for, without a fixed appointment — registration and
  enrollment steps, applications and their deadlines, forms, fees, official
  notices about their course, program, account, accommodation or employment, a
  request addressed to them, an interview or slot to arrange. A stated deadline
  or required action makes a mail important even when it arrives as a bulk
  notice, newsletter or invitation.
- "info" (green #67C23A): optional or FYI — event announcements the recipient MAY
  attend, lectures and seminars without attendance requirements, club activities,
  general notices, newsletters, postings with no action and no deadline for them.
- "ad" (gray #909399): unusable mail only — unsolicited sales and promotions,
  spam, and automated system chatter without usable content (routine login
  reminders, password-expiry nudges, delivery status).

Rules:
1. Ask "is there anything this person has to act on, answer or track?" If yes, it
   is at least "important". Bulk, automated, promotional-looking or mass-mailed is
   NOT a reason to choose info or ad.
2. Before choosing "ad", the reason must name what makes the mail unusable. A mail
   announcing an event, deadline, opportunity, recruitment, result or service
   change is at least "info", and "important" once action or a deadline is
   involved.
3. Routine login/security/password and delivery notices are ALWAYS "ad". Lectures
   and seminars without mandatory attendance are "info" even with a date; raise
   them only when attendance is required of THIS recipient or an action (register,
   RSVP, submit) is demanded.
4. reply_required=true ONLY when the sender explicitly expects an answer.
5. Every stated obligation yields an action item: deadline, registration,
   submission, payment, appointment, exam, interview, pickup, or an event with a
   date — including inside announcements. Parse due_at from the mail (ISO-8601
   with offset); with only a date use 09:00 in the mail's timezone and say so in
   the notes; never invent a date (leave action_items empty instead).
   action_type ∈ {"exam","meeting","errand","other"}: exam = tests/quizzes;
   meeting = scheduled meetings, calls, defenses, interviews, events to attend;
   errand = pickups, payments, registrations, applications, submissions; other
   only when none fit.
6. reason must agree with urgency: never describe an obligation inside an "info"
   or "ad" reason — raise the level instead.
7. If a recipient profile is given, it decides relevance: mail matching what they
   care about is at least "info" (important/urgent when it carries action or a
   deadline), mail in the categories they ignore is "ad".
8. Feedback notes are narrow, kind-scoped preferences for re-ranking similar
   mail; they never override rules 1-6 and may never turn a mail with an action,
   deadline or obligation into "ad" or "info".
9. urgency is exactly one of the English tokens ad, info, important, urgent.
10. Output ONLY one JSON object, no prose or fences:
{
  "summary": "one or two sentences",
  "urgency": "ad|info|important|urgent",
  "reason": "short reason for the level",
  "reply_required": true,
  "suggested_reply": "draft if reply_required else empty",
  "action_items": [
    {"summary": "what must be done", "action_type": "exam|meeting|errand|other",
     "due_at": "ISO-8601 with offset", "due_end": "ISO-8601 or null",
     "notes": "what to bring or prepare"}
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
            notes = _prompt_guidelines(context.feedback_guidelines)
        else:
            notes = ""
        if notes:
            user += (
                "\nUser feedback notes from earlier corrections. Treat them as "
                "narrow, kind-scoped preferences: they may re-rank mail of the "
                "same kind (a promotion the user rejected is ad rather than "
                "info), but they never change what the mail actually asks for — "
                "mail that carries an action, a deadline or an obligation of the "
                "recipient keeps the level the rules above give it:\n"
                f"{notes}\n"
            )
        if context.user_profile:
            user += (
                "\nRecipient profile, written by the recipient themselves "
                "(authoritative for what matters to them; mail matching what they "
                "care about is at least info, mail in the categories they ignore "
                "is ad):\n"
                f"{context.user_profile}\n"
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
        summary = payload.summary[: self._max_summary_chars].strip()
        analysis = MailAnalysis(
            summary=summary or mail.subject,
            urgency=urgency,
            summary_is_fallback=not bool(summary),
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
