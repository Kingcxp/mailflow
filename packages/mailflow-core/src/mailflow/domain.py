"""Provider-independent domain types for MailFlow.

Everything in this module is transport- and UI-neutral: no concrete mail
provider, LLM client, storage backend or terminal is imported here. Runtime
snapshot types intentionally describe *registrations* rather than concrete
adapter objects so that a CLI, TUI or bot host can render the system state
without importing plugin packages.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Urgency
# ---------------------------------------------------------------------------


class Urgency(StrEnum):
    """Four-level mail importance contract.

    Colors are part of the public contract and are reused by the CLI, TUI and
    notification adapters.
    """

    AD = "ad"  # gray: irrelevant advertising / junk
    INFO = "info"  # green: useful but not time-critical (e.g. lecture notice)
    IMPORTANT = "important"  # orange: needs reading (e.g. verification codes)
    URGENT = "urgent"  # red: must be handled now or at a specific time

    @property
    def color(self) -> str:
        colors = {
            Urgency.AD: "#909399",
            Urgency.INFO: "#67C23A",
            Urgency.IMPORTANT: "#E6A23C",
            Urgency.URGENT: "#F56C6C",
        }
        return colors[self]

    @property
    def rank(self) -> int:
        ranks = {
            Urgency.AD: 0,
            Urgency.INFO: 1,
            Urgency.IMPORTANT: 2,
            Urgency.URGENT: 3,
        }
        return ranks[self]


def parse_urgency(value: str | None) -> Urgency:
    """Parse a user/LLM-provided urgency string, defaulting to INFO."""
    if value is None:
        return Urgency.INFO
    try:
        return Urgency(value.strip().lower())
    except ValueError:
        # Accept common synonyms produced by imperfect LLM endpoints.
        normalized = value.strip().lower().replace("_", "-")
        synonyms: dict[str, Urgency] = {
            "junk": Urgency.AD,
            "spam": Urgency.AD,
            "ads": Urgency.AD,
            "advertisement": Urgency.AD,
            "normal": Urgency.INFO,
            "low": Urgency.INFO,
            "medium": Urgency.IMPORTANT,
            "high": Urgency.URGENT,
            "critical": Urgency.URGENT,
            "urgently": Urgency.URGENT,
        }
        return synonyms.get(normalized, Urgency.INFO)


# ---------------------------------------------------------------------------
# Mail message
# ---------------------------------------------------------------------------


class MailAddress(BaseModel):
    name: str = ""
    address: str

    @property
    def display(self) -> str:
        if self.name:
            return f"{self.name} <{self.address}>"
        return self.address

    @classmethod
    def parse(cls, raw: str) -> MailAddress:
        """Parse a RFC-ish ``Name <addr>`` or bare ``addr`` string."""
        raw = raw.strip()
        if not raw:
            return MailAddress(address="")
        if raw.endswith(">") and "<" in raw:
            name, _, addr = raw.rpartition("<")
            return MailAddress(name=name.strip(" \"'"), address=addr[:-1].strip())
        return MailAddress(address=raw)


class Attachment(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"
    size: int = 0
    content_id: str | None = None
    # Payload is kept out of the persisted JSON by the storage backend.
    data: bytes | None = None


class MailMessage(BaseModel):
    """Normalized, provider-independent mail.

    ``body_text``/``body_html`` are the original contents; analysis lives in a
    separate ``MailAnalysis`` record.
    """

    message_id: str
    account_id: str
    subject: str
    sender: MailAddress
    recipients: list[MailAddress] = Field(default_factory=list)
    cc: list[MailAddress] = Field(default_factory=list)
    date: datetime  # send time as reported by the provider
    received_at: datetime  # normalized fetch time, always timezone-aware UTC
    body_text: str = ""
    body_html: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    thread_id: str | None = None
    in_reply_to: str | None = None
    provider: str = ""  # source plugin id that produced this message
    provider_message_id: str = ""  # provider-specific stable id

    def normalized_message_id(self) -> str:
        """Stable identity: provider id when present, else content digest."""
        if self.provider_message_id:
            return self.provider_message_id
        digest = hashlib.sha256(
            (
                f"{self.sender.address}|{self.subject}|{self.date.isoformat()}"
                f"|{self.account_id}"
            ).encode()
        ).hexdigest()[:24]
        return digest

    @property
    def is_read(self) -> bool:
        return False  # read state is a per-record preference, not message data


class ProcessorNote(BaseModel):
    """Trail entry produced by the processing pipeline."""

    processor_id: str
    plugin_id: str
    status: str  # success | failed | skipped
    message: str = ""
    started_at: datetime
    finished_at: datetime

    @property
    def duration_ms(self) -> int:
        return max(0, int((self.finished_at - self.started_at).total_seconds() * 1000))


class ActionItem(BaseModel):
    """A timed task extracted from a mail (exam, meeting, errand, ...)."""

    item_id: str
    mail_id: str
    summary: str
    action_type: str  # exam | meeting | errand | other (free-form labels allowed)
    due_at: datetime
    due_end: datetime | None = None
    notes: str = ""

    @property
    def time_range(self) -> str:
        fmt = "%Y-%m-%d %H:%M"
        if self.due_end is None or self.due_end == self.due_at:
            return self.due_at.strftime(fmt)
        return f"{self.due_at.strftime(fmt)} ~ {self.due_end.strftime(fmt)}"


class MailAnalysis(BaseModel):
    """Structured interpretation of a mail produced by the processor chain."""

    summary: str
    urgency: Urgency
    reason: str = ""
    reply_required: bool = False
    suggested_reply: str = ""
    action_items: list[ActionItem] = Field(default_factory=list)
    notes: str = ""
    backend: str = ""  # llm backend plugin id actually used ("" if rule-based)


# ---------------------------------------------------------------------------
# Stored record
# ---------------------------------------------------------------------------


class MailRecord(BaseModel):
    """A mail plus its accumulated analysis, stored by the storage backend."""

    record_id: str
    mail: MailMessage
    auto_urgency: Urgency = Urgency.INFO
    manual_urgency: Urgency | None = None
    analysis: MailAnalysis | None = None
    processor_notes: list[ProcessorNote] = Field(default_factory=list)
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def effective_urgency(self) -> Urgency:
        """Manual override wins; reset (None) restores the automatic value."""
        return self.manual_urgency if self.manual_urgency is not None else self.auto_urgency

    @property
    def summary(self) -> str:
        if self.analysis is not None:
            return self.analysis.summary
        return self.mail.subject

    @property
    def action_items(self) -> list[ActionItem]:
        if self.analysis is None:
            return []
        return self.analysis.action_items


class TrashRecord(BaseModel):
    """Recoverable copy of a mail held in the trash store."""

    record_id: str
    mail: MailMessage
    auto_urgency: Urgency
    manual_urgency: Urgency | None
    analysis: MailAnalysis | None
    processor_notes: list[ProcessorNote] = Field(default_factory=list)
    deleted_at: datetime
    expires_at: datetime  # purge time; compared against deletion time only

    def to_mail_record(self) -> MailRecord:
        return MailRecord(
            record_id=self.record_id,
            mail=self.mail,
            auto_urgency=self.auto_urgency,
            manual_urgency=self.manual_urgency,
            analysis=self.analysis,
            processor_notes=self.processor_notes,
            received_at=self.mail.received_at,
        )


# ---------------------------------------------------------------------------
# Reply workflow
# ---------------------------------------------------------------------------


class ReplyState(StrEnum):
    DRAFT = "draft"
    PREPARED = "prepared"
    SENT = "sent"
    CANCELLED = "cancelled"


class ReplyDraft(BaseModel):
    """Editable reply; confirm() requires a short-lived prepared token."""

    draft_id: str
    mail_id: str
    account_id: str
    to: MailAddress
    subject: str
    body: str
    state: ReplyState = ReplyState.DRAFT
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    token: str | None = None
    token_expires_at: datetime | None = None

    def is_confirmation_valid(self, token: str, now: datetime | None = None) -> bool:
        if self.state != ReplyState.PREPARED or self.token is None or self.token_expires_at is None:
            return False
        if token != self.token:
            return False
        now = now or datetime.now(UTC)
        return now <= self.token_expires_at


# ---------------------------------------------------------------------------
# Registration / runtime snapshots (UI- and transport-neutral)
# ---------------------------------------------------------------------------


class ComponentKind(StrEnum):
    MAIL_SOURCE = "mail_source"
    MAIL_PROCESSOR = "mail_processor"
    LLM_BACKEND = "llm_backend"
    NOTIFIER = "notifier"
    STORAGE = "storage"


class PluginSnapshot(BaseModel):
    plugin_id: str
    name: str
    version: str = ""
    description: str = ""
    kinds: list[ComponentKind] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)


class ComponentSnapshot(BaseModel):
    component_id: str
    kind: ComponentKind
    plugin_id: str


class AccountSnapshot(BaseModel):
    account_id: str
    provider: str  # source plugin id
    email: str
    enabled: bool
    status: str = "stopped"  # starting | running | stopped | error
    error: str | None = None


class LLMSnapshot(BaseModel):
    llm_id: str
    name: str
    backend: str  # llm backend plugin id
    model: str
    base_url: str = ""
    default: bool = False


class ProcessorBindingSnapshot(BaseModel):
    processor_id: str
    plugin_id: str
    priority: int
    llm_id: str | None = None
    fallback_llm_ids: list[str] = Field(default_factory=list)


class RuntimeSnapshot(BaseModel):
    version: str = ""
    language: str = "en"
    timezone: str = "UTC"
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    plugins: list[PluginSnapshot] = Field(default_factory=list)
    components: list[ComponentSnapshot] = Field(default_factory=list)
    accounts: list[AccountSnapshot] = Field(default_factory=list)
    llms: list[LLMSnapshot] = Field(default_factory=list)
    processors: list[ProcessorBindingSnapshot] = Field(default_factory=list)
    storage: str | None = None

    def plugin(self, plugin_id: str) -> PluginSnapshot | None:
        return next((p for p in self.plugins if p.plugin_id == plugin_id), None)

    def components_of(self, plugin_id: str) -> list[ComponentSnapshot]:
        return [c for c in self.components if c.plugin_id == plugin_id]


# ---------------------------------------------------------------------------
# Command responses (transport-neutral: style is metadata, not ANSI bytes)
# ---------------------------------------------------------------------------


class StyleSpan(BaseModel):
    text: str
    style: str = ""


class CommandResponse(BaseModel):
    ok: bool
    spans: list[StyleSpan] = Field(default_factory=list)
    text: str = ""  # plain rendering for transports without rich support

    @classmethod
    def plain(cls, text: str, *, ok: bool = True) -> CommandResponse:
        return cls(ok=ok, spans=[StyleSpan(text=text)], text=text)

    @classmethod
    def rich(cls, spans: list[tuple[str, str]], *, ok: bool = True) -> CommandResponse:
        return cls(
            ok=ok,
            spans=[StyleSpan(text=text, style=style) for text, style in spans],
            text="".join(text for text, _ in spans),
        )

    def render(self) -> str:
        return self.text


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    """Normalize any aware/naive datetime to UTC-aware; naive assumed UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "AccountSnapshot",
    "ActionItem",
    "Attachment",
    "CommandResponse",
    "ComponentKind",
    "ComponentSnapshot",
    "LLMSnapshot",
    "MailAddress",
    "MailAnalysis",
    "MailMessage",
    "MailRecord",
    "PluginSnapshot",
    "ProcessorBindingSnapshot",
    "ProcessorNote",
    "ReplyDraft",
    "ReplyState",
    "RuntimeSnapshot",
    "StyleSpan",
    "TrashRecord",
    "Urgency",
    "parse_urgency",
    "to_utc",
    "utcnow",
]
