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
from typing import Any, Protocol

from pydantic import BaseModel, Field

from mailflow.domain import (
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


class LLMCompletion(BaseModel):
    """A chat completion from any backend."""

    text: str
    model: str = ""
    backend: str = ""  # backend plugin id, stamped by the router
    llm_id: str = ""  # named llm actually used, stamped by the router
    raw: dict[str, Any] = Field(default_factory=dict)


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
    ) -> LLMCompletion:
        ...


class LLMBackend(Protocol):
    """A concrete chat-completions transport."""

    backend_id: str

    async def chat(
        self,
        messages: list[MessageDict],
        *,
        temperature: float | None = None,
        options: dict[str, Any] | None = None,
    ) -> LLMCompletion:
        ...


class ProcessingContext(BaseModel):
    """Per-mail context passed to every processor."""

    account_id: str
    timezone: str = "UTC"
    options: dict[str, Any] = Field(default_factory=dict)
    now: datetime | None = None  # injected clock for deterministic processors


class ProcessorDecision(StrEnum):
    CONTINUE = "continue"
    STOP = "stop"


class ProcessorResult(BaseModel):
    """What one processor contributes to the pipeline."""

    decision: ProcessorDecision = ProcessorDecision.CONTINUE
    analysis: MailAnalysis | None = None  # partial overlay, merged by the pipeline
    llm_used: str = ""  # named llm actually used
    llm_backend: str = ""  # backend plugin id actually used
    notes: list[str] = Field(default_factory=list)


class MailProcessor(Protocol):
    """One step in the ordered processing chain."""

    processor_id: str

    async def process(self, mail: MailMessage, context: ProcessingContext) -> ProcessorResult:
        ...


class Notifier(Protocol):
    """Delivers an already-computed mail analysis to a channel."""

    async def notify(self, record: MailRecord) -> None:
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
    async def set_manual_urgency(self, record_id: str, urgency: Urgency | None) -> MailRecord | None: ...
    async def delete_mail(self, record_id: str) -> None:  # moves full record to trash
        ...

    # -- trash (recovery) ----------------------------------------------------
    async def list_trash(self) -> list[TrashRecord]: ...
    async def restore_from_trash(self, record_id: str) -> MailRecord | None: ...
    async def purge_trash(self, before: datetime) -> None: ...
    async def cleanup_mail(self, before: datetime) -> None:  # old active -> trash
        ...

    # -- reply drafts --------------------------------------------------------
    async def save_draft(self, draft: ReplyDraft) -> None: ...
    async def get_draft(self, draft_id: str) -> ReplyDraft | None: ...
    async def delete_draft(self, draft_id: str) -> None: ...

    # -- preferences (e.g. persisted language) --------------------------------
    async def get_preference(self, key: str) -> str | None: ...
    async def set_preference(self, key: str, value: str) -> None: ...


__all__ = [
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
