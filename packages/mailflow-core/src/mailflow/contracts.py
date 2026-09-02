"""Typed component contracts for MailFlow.

Protocols define the seams every plugin implements. The LLM router is typed as
a Protocol here so that ``registry`` (Stage 03) never imports the concrete
router implementation (Stage 05) — the dependency direction is contracts →
domain only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from mailflow.domain import (
    ActionItem,
    MailAddress,
    MailAnalysis,
    MailMessage,
    MailRecord,
    ReplyDraft,
    TrashRecord,
    Urgency,
)

MessageDict = dict[str, str]

MailEmitter = Callable[[MailMessage], Awaitable[None]]


@runtime_checkable
class MailSource(Protocol):
    """A mail provider adapter: emits normalized messages, can reply."""

    async def run(self, emit: MailEmitter, stop_event: asyncio.Event) -> None:
        """Stream messages into ``emit`` until ``stop_event`` is set."""
        ...

    async def send_reply(self, mail_id: str, draft: ReplyDraft) -> None:
        """Send a confirmed reply for ``mail_id`` using this provider."""
        ...

    async def close(self) -> None:
        """Release provider resources."""
        ...


@runtime_checkable
class HistoryCapableSource(Protocol):
    """A mail source that can list already-received mail on demand.

    Optional capability: the TUI's mailbox browser offers a per-account
    history view only for sources implementing it. ``run`` keeps owning the
    live stream — ``fetch_history`` never emits, it just returns messages so
    the caller can decide which ones to push through the pipeline.
    """

    async def fetch_history(self, limit: int = 50, offset: int = 0) -> list[MailMessage]:
        """Return up to ``limit`` messages, newest first, skipping ``offset``."""
        ...


class LLMCompletion(BaseModel):
    """A chat completion from any backend."""

    text: str
    model: str = ""
    backend: str = ""  # backend plugin id, stamped by the router
    llm_id: str = ""  # named llm actually used, stamped by the router
    raw: dict[str, Any] = Field(default_factory=lambda: {})


class LLMRouter(Protocol):
    """Named LLM routing with ordered fallback (implemented in mailflow.llm)."""

    async def chat(
        self,
        messages: list[MessageDict],
        *,
        primary: str,
        fallback: list[str] | None = None,
        temperature: float | None = None,
        options: dict[str, Any] | None = None,
    ) -> LLMCompletion: ...


class LLMBackend(Protocol):
    """A concrete chat-completions transport."""

    backend_id: str

    async def chat(
        self,
        messages: list[MessageDict],
        *,
        temperature: float | None = None,
        options: dict[str, Any] | None = None,
    ) -> LLMCompletion: ...


class ProcessingContext(BaseModel):
    """Per-mail context passed to every processor."""

    account_id: str
    timezone: str = "UTC"
    options: dict[str, Any] = Field(default_factory=lambda: {})
    now: datetime | None = None  # injected clock for deterministic processors
    feedback_guidelines: str = ""  # user notes on what to ignore, for the LLM


class ProcessorDecision(StrEnum):
    CONTINUE = "continue"
    STOP = "stop"


class ProcessorResult(BaseModel):
    """What one processor contributes to the pipeline."""

    decision: ProcessorDecision = ProcessorDecision.CONTINUE
    analysis: MailAnalysis | None = None  # partial overlay, merged by the pipeline
    llm_used: str = ""  # named llm actually used
    llm_backend: str = ""  # backend plugin id actually used
    notes: list[str] = Field(default_factory=lambda: [])


class MailProcessor(Protocol):
    """One step in the ordered processing chain."""

    processor_id: str

    async def process(self, mail: MailMessage, context: ProcessingContext) -> ProcessorResult: ...


class LLMEnhancer(Protocol):
    """Extends the built-in LLM analysis within a bounded scope.

    Processor plugins implement this to customize how the built-in
    ``llm-importance`` processor talks to the model and what it keeps:
    append to the system prompt, inject extra chat messages, and adjust
    the parsed analysis afterwards. Every hook is optional; the defaults
    are no-ops.
    """

    def system_prompt(self, base: str) -> str:
        """Return the system prompt with your additions appended."""
        return base

    def extra_messages(self, mail: MailMessage, context: ProcessingContext) -> list[dict[str, str]]:
        """Additional messages appended after the user message."""
        return []

    def post_process(
        self,
        analysis: MailAnalysis,
        mail: MailMessage,
        context: ProcessingContext,
    ) -> MailAnalysis | None:
        """Adjust the parsed analysis; return ``None`` to keep it unchanged."""
        return None


class Notifier(Protocol):
    """Delivers an already-computed mail analysis to a channel."""

    async def notify(self, record: MailRecord) -> None: ...


class GatewayInstance(BaseModel):
    """One managed gateway instance: provider + instance id + state."""

    provider: str  # gateway provisioner component id (e.g. "napcat")
    instance_id: str  # unique per provider (e.g. "napcat-1")
    status: str = "unknown"  # detected | installing | starting | running | error | stopped
    error: str = ""
    endpoint: str = ""  # http base URL once running (e.g. http://127.0.0.1:3001)
    extra: dict[str, Any] = Field(default_factory=lambda: {})


class GatewayProvisioner(Protocol):
    """Installs, starts and supervises one chat-platform gateway process.

    The provisioner owns the *how* (download NapCat, npm-install WeChaty,
    launch the child); ``mailflow.gateway.GatewayManager`` owns the
    lifecycle (persist state, restart on crash, stop on shutdown).
    Implementations are plugins (component kind GATEWAY_PROVISIONER), so a
    new chat platform is a marketplace install, not a core change.
    """

    async def detect(self) -> str:
        """Return a status line: whether the gateway runtime is installed
        and/or already running (used to prefill the guide)."""
        ...

    async def install(self, instance_id: str, options: dict[str, Any]) -> None:
        """Install the gateway runtime for one instance. No-op when already
        installed; raises with a clear message on failure."""
        ...

    async def start(self, instance_id: str, options: dict[str, Any]) -> GatewayInstance:
        """Launch the gateway process for one instance and wait until its
        HTTP endpoint answers; returns the running instance (endpoint set)."""
        ...

    async def stop(self, instance_id: str) -> None:
        """Terminate the gateway process for one instance (no-op when not
        running)."""
        ...

    async def status(self, instance_id: str) -> GatewayInstance:
        """Current state: running endpoint, or error with a readable
        message."""
        ...

    async def qr(self, instance_id: str) -> str:
        """A QR payload (base64 PNG or URL) for the login step; '' when the
        platform has no in-TUI QR flow."""
        ...


class StorageBackend(Protocol):
    """Durable persistence for records, trash, drafts and preferences."""

    async def initialize(self) -> None: ...
    async def close(self) -> None: ...

    # -- active mail ---------------------------------------------------------
    async def save_mail(self, record: MailRecord) -> None: ...
    async def get_mail(self, record_id: str) -> MailRecord | None: ...
    async def list_mails(self, limit: int | None = None) -> list[MailRecord]: ...
    async def count_mails(self) -> int: ...
    async def set_manual_urgency(
        self, record_id: str, urgency: Urgency | None
    ) -> MailRecord | None: ...
    async def update_mail_analysis(
        self,
        record_id: str,
        *,
        urgency: Urgency | None = None,
        summary: str | None = None,
        reason: str | None = None,
    ) -> MailRecord | None: ...
    async def delete_mail(self, record_id: str) -> None:  # moves full record to trash
        ...

    # -- trash (recovery) ----------------------------------------------------
    async def list_trash(self) -> list[TrashRecord]: ...
    async def restore_from_trash(self, record_id: str) -> MailRecord | None: ...
    async def purge_trash(self, before: datetime) -> int: ...
    async def cleanup_mail(self, before: datetime) -> int:  # old active -> trash
        ...

    # -- reply drafts --------------------------------------------------------
    async def save_draft(self, draft: ReplyDraft) -> None: ...
    async def get_draft(self, draft_id: str) -> ReplyDraft | None: ...
    async def delete_draft(self, draft_id: str) -> None: ...

    # -- preferences (e.g. persisted language) --------------------------------
    async def get_preference(self, key: str) -> str | None: ...
    async def set_preference(self, key: str, value: str) -> None: ...

    # -- custom action items (user-created; reminder-capable) -----------------
    async def save_custom_action(self, item: ActionItem) -> None: ...
    async def list_custom_actions(self) -> list[ActionItem]: ...
    async def delete_custom_action(self, item_id: str) -> bool: ...


__all__ = [
    "GatewayInstance",
    "GatewayProvisioner",
    "HistoryCapableSource",
    "LLMBackend",
    "LLMCompletion",
    "LLMRouter",
    "MailAddress",
    "MailEmitter",
    "MailProcessor",
    "MailSource",
    "MessageDict",
    "Notifier",
    "ProcessingContext",
    "ProcessorDecision",
    "ProcessorResult",
    "StorageBackend",
]
