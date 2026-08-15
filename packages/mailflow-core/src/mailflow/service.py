"""The public MailFlow service facade.

One object exposes everything a CLI, TUI or chat-bot host needs: runtime
snapshots, mail/action/trash queries, urgency mutations, persistent language,
and the confirmed reply workflow. ``start_service()`` is the single entry
point that composes configuration, plugins, storage, LLMs, processors,
sources, notifiers, events, logging and the runtime.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

from mailflow import __version__
from mailflow.config import LLMConfig, MailFlowConfig, NotifierConfig, load_config
from mailflow.contracts import (
    LLMBackend,
    LLMRouter,
    MailProcessor,
    MailSource,
    Notifier,
    StorageBackend,
)
from mailflow.domain import (
    AccountSnapshot,
    ActionItem,
    LLMSnapshot,
    MailRecord,
    ProcessorBindingSnapshot,
    ReplyDraft,
    ReplyState,
    RuntimeSnapshot,
    TrashRecord,
    Urgency,
    utcnow,
)
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.llm import LLMRouterImpl
from mailflow.logging import LoggingRuntime, configure_logging
from mailflow.pipeline import PipelineEngine, build_bindings
from mailflow.plugins import PluginManager
from mailflow.registry import ComponentRegistry
from mailflow.runtime import MailFlowRuntime

logger = logging.getLogger("mailflow.service")

_LANGUAGE_PREFERENCE = "language"
_REPLY_TOKEN_TTL = timedelta(minutes=10)


class MailFlowService:
    """Embeds one fully configured MailFlow runtime."""

    def __init__(
        self,
        *,
        config: MailFlowConfig,
        registry: ComponentRegistry,
        plugin_manager: PluginManager,
        storage: StorageBackend,
        sources: dict[str, MailSource],  # keyed by account_id
        router: LLMRouter,
        pipeline: PipelineEngine,
        notifiers: list[Notifier],
        notifier_configs: list[NotifierConfig],
        events: EventBus,
        i18n: I18n,
        logging_runtime: LoggingRuntime | None = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.plugin_manager = plugin_manager
        self.storage = storage
        self.sources = sources
        self.router = router
        self.pipeline = pipeline
        self.events = events
        self.i18n = i18n
        self._logging_runtime = logging_runtime

        self.runtime = MailFlowRuntime(
            config,
            sources=sources,
            pipeline=pipeline,
            storage=storage,
            notifiers=notifiers,
            notifier_configs=notifier_configs,
            events=events,
            account_configs=config.accounts,
        )
        self._started = False
        self._stopped_event = asyncio.Event()
        self._stop_task: asyncio.Task[Any] | None = None
        self.commands: Any | None = None  # CommandRouter wired by mailflow.commands

    # -- lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        await self.storage.initialize()
        await self._load_persisted_language()
        await self.runtime.start()
        self._started = True
        self._stopped_event = asyncio.Event()
        logger.info("mailflow service started (version %s)", __version__)

    async def stop(self) -> None:
        self._stopped_event.set()
        await self.runtime.stop()
        await self.storage.close()
        self._started = False
        logger.info("mailflow service stopped")

    async def wait(self) -> None:
        """Block until the service is stopped (useful for standalone hosts)."""
        await self._stopped_event.wait()

    async def wait_idle(self, timeout_seconds: float = 5.0) -> bool:
        """Wait until all queued mails have been processed."""
        return await self.runtime.wait_idle(timeout_seconds=timeout_seconds)

    def on(self, event: str, handler: Callable[..., Awaitable[None]]) -> Callable[[], None]:
        return self.events.subscribe(event, handler)

    @property
    def started(self) -> bool:
        return self._started

    # -- queries -------------------------------------------------------------------

    def snapshot(self) -> RuntimeSnapshot:
        account_snapshots = [
            AccountSnapshot(
                account_id=account.account_id,
                provider=account.provider,
                email=account.email,
                enabled=account.enabled,
                status=self.runtime.account_status(account.account_id),
                error=self.runtime.account_error(account.account_id),
            )
            for account in self.config.accounts
        ]
        return RuntimeSnapshot(
            version=__version__,
            language=self.i18n.language,
            timezone=self.config.general.timezone,
            started_at=self.runtime.started_at or utcnow(),
            plugins=self.plugin_manager.snapshots(self.registry),
            components=self.registry.snapshots(),
            accounts=account_snapshots,
            llms=[
                LLMSnapshot(
                    llm_id=llm.llm_id,
                    name=llm.name or llm.llm_id,
                    backend=llm.provider,
                    model=llm.model,
                    base_url=llm.base_url,
                    default=llm.default,
                )
                for llm in self.config.llms
            ],
            processors=[
                ProcessorBindingSnapshot(
                    processor_id=processor.processor_id,
                    plugin_id=self.registry.plugin_for(processor.processor_id) or "",
                    priority=processor.priority,
                    llm_id=processor.llm,
                    fallback_llm_ids=list(processor.fallback_llms),
                )
                for processor in self.config.processors
                if processor.enabled
            ],
            storage=self.config.storage.provider,
        )

    async def list_mails(self, limit: int | None = None) -> list[MailRecord]:
        return await self.storage.list_mails(limit=limit)

    async def get_mail(self, record_id: str) -> MailRecord | None:
        return await self.storage.get_mail(record_id)

    async def count_mails(self) -> int:
        return await self.storage.count_mails()

    async def list_actions(self) -> list[ActionItem]:
        items: list[ActionItem] = []
        for record in await self.storage.list_mails():
            items.extend(record.action_items)
        return sorted(items, key=lambda item: item.due_at)

    async def list_trash(self) -> list[TrashRecord]:
        return await self.storage.list_trash()

    # -- mutations -------------------------------------------------------------------

    async def set_mail_urgency(self, record_id: str, urgency: Urgency | None) -> MailRecord | None:
        """Set or reset (None) the manual urgency override."""
        record = await self.storage.set_manual_urgency(record_id, urgency)
        if record is not None:
            await self.events.emit("mail.urgency.changed", record_id=record_id, urgency=urgency)
        return record

    async def delete_mail(self, record_id: str) -> bool:
        """Move a mail to trash (recoverable); returns False when unknown."""
        record = await self.storage.get_mail(record_id)
        if record is None:
            return False
        await self.storage.delete_mail(record_id)
        await self.events.emit("mail.deleted", record_id=record_id)
        return True

    async def restore_mail(self, record_id: str) -> MailRecord | None:
        return await self.storage.restore_from_trash(record_id)

    async def run_cleanup(self) -> tuple[int, int]:
        moved = await self.storage.cleanup_mail(
            utcnow() - timedelta(days=self.config.general.mail_retention_days)
        )
        purged = await self.storage.purge_trash(
            utcnow() - timedelta(days=self.config.general.trash_retention_days)
        )
        return moved, purged

    # -- language -------------------------------------------------------------------------

    async def _load_persisted_language(self) -> None:
        try:
            stored = await self.storage.get_preference(_LANGUAGE_PREFERENCE)
        except Exception as exc:
            logger.debug("could not read language preference: %s", exc)
            return
        if stored and stored in self.i18n.available_codes():
            self.i18n.set_language(stored)

    async def get_language(self) -> str:
        return self.i18n.language

    async def set_language(self, code: str) -> None:
        self.i18n.set_language(code)  # raises KeyError for unknown packs
        await self.storage.set_preference(_LANGUAGE_PREFERENCE, code)
        await self.events.emit("language.changed", language=code)

    def available_languages(self) -> list[str]:
        return self.i18n.available_codes()

    def t(self, key: str, **params: Any) -> str:
        return self.i18n.t(key, **params)

    # -- reply workflow ---------------------------------------------------------------------

    async def create_reply(self, mail_id: str) -> ReplyDraft:
        record = await self.storage.get_mail(mail_id)
        if record is None:
            raise KeyError(f"mail {mail_id} not found")
        draft = ReplyDraft(
            draft_id=uuid4().hex[:16],
            mail_id=mail_id,
            account_id=record.mail.account_id,
            to=record.mail.sender,
            subject=f"Re: {record.mail.subject}",
            body=record.analysis.suggested_reply if record.analysis else "",
        )
        await self.storage.save_draft(draft)
        await self.events.emit("reply.created", draft_id=draft.draft_id, mail_id=mail_id)
        return draft

    async def get_draft(self, draft_id: str) -> ReplyDraft | None:
        return await self.storage.get_draft(draft_id)

    async def edit_draft(self, draft_id: str, subject: str, body: str) -> ReplyDraft:
        draft = await self._require_draft(draft_id)
        if draft.state in (ReplyState.SENT, ReplyState.CANCELLED):
            raise ValueError(f"draft {draft_id} is {draft.state.value}; cannot edit")
        draft.subject = subject
        draft.body = body
        draft.updated_at = utcnow()
        if draft.state == ReplyState.PREPARED:
            draft.state = ReplyState.DRAFT  # editing invalidates the confirmation token
            draft.token = None
            draft.token_expires_at = None
        await self.storage.save_draft(draft)
        return draft

    async def prepare_reply(self, draft_id: str) -> ReplyDraft:
        draft = await self._require_draft(draft_id)
        if draft.state in (ReplyState.SENT, ReplyState.CANCELLED):
            raise ValueError(f"draft {draft_id} is {draft.state.value}; cannot prepare")
        draft.token = secrets.token_urlsafe(16)
        draft.token_expires_at = utcnow() + _REPLY_TOKEN_TTL
        draft.state = ReplyState.PREPARED
        draft.updated_at = utcnow()
        await self.storage.save_draft(draft)
        return draft

    async def confirm_reply(self, draft_id: str, token: str) -> ReplyDraft:
        draft = await self._require_draft(draft_id)
        if not draft.is_confirmation_valid(token):
            raise PermissionError("invalid or expired confirmation token")
        # Persist SENT before sending: a crash between send and save cannot
        # cause a double send, because the token is consumed here.
        draft.state = ReplyState.SENT
        draft.token = None
        draft.token_expires_at = None
        draft.updated_at = utcnow()
        await self.storage.save_draft(draft)
        source = self.sources.get(draft.account_id)
        if source is None:
            raise RuntimeError(f"no source for account {draft.account_id!r}")
        try:
            await source.send_reply(draft.mail_id, draft)
        except Exception:
            # Revert to an un-tokenized draft so the user must prepare again.
            draft.state = ReplyState.DRAFT
            draft.updated_at = utcnow()
            await self.storage.save_draft(draft)
            raise
        await self.events.emit("reply.sent", draft_id=draft_id, mail_id=draft.mail_id)
        return draft

    async def cancel_reply(self, draft_id: str) -> ReplyDraft:
        draft = await self._require_draft(draft_id)
        if draft.state == ReplyState.SENT:
            raise ValueError(f"draft {draft_id} was already sent; cannot cancel")
        draft.state = ReplyState.CANCELLED
        draft.token = None
        draft.token_expires_at = None
        draft.updated_at = utcnow()
        await self.storage.save_draft(draft)
        return draft

    async def _require_draft(self, draft_id: str) -> ReplyDraft:
        draft = await self.storage.get_draft(draft_id)
        if draft is None:
            raise KeyError(f"draft {draft_id} not found")
        return draft


# ---------------------------------------------------------------------------
# Startup composition
# ---------------------------------------------------------------------------


def _build_sources(config: MailFlowConfig, registry: ComponentRegistry) -> dict[str, MailSource]:
    sources: dict[str, MailSource] = {}
    for account in config.accounts:
        factory = registry.source_factory(account.provider)
        sources[account.account_id] = factory(account)
    return sources


def _build_llms(
    config: MailFlowConfig, registry: ComponentRegistry
) -> tuple[dict[str, LLMBackend], dict[str, LLMConfig]]:
    backends: dict[str, LLMBackend] = {}
    for llm_config in config.llms:
        factory = registry.llm_factory(llm_config.provider)
        backends[llm_config.llm_id] = factory(llm_config)
    configs = {llm.llm_id: llm for llm in config.llms}
    return backends, configs


def _build_processors(
    config: MailFlowConfig,
    registry: ComponentRegistry,
    router: LLMRouter,
) -> PipelineEngine:
    processor_configs = [p for p in config.processors if p.enabled]
    processors: dict[str, MailProcessor] = {}
    plugin_of: dict[str, str] = {}
    for processor_config in processor_configs:
        factory = registry.processor_factory(processor_config.provider)
        processors[processor_config.processor_id] = factory(processor_config, router)
        plugin_of[processor_config.processor_id] = (
            registry.plugin_for(processor_config.provider) or ""
        )
    bindings = build_bindings(processor_configs, processors, plugin_of)
    return PipelineEngine(bindings, router=router)


async def start_service(
    config: MailFlowConfig | None = None,
    config_path: str | Path | None = None,
    *,
    plugin_manager: PluginManager | None = None,
    discover_plugins: bool = True,
    output: TextIO | None = None,
    extra_log_handlers: Sequence[logging.Handler] | None = None,
    enable_logging: bool = True,
) -> MailFlowService:
    """Start the complete service; the single entry point for every host.

    ``output`` redirects the rich console stream; ``extra_log_handlers`` lets
    a host (e.g. a TUI or bot framework) inject its own log sinks.
    """
    if config is None:
        config = load_config(config_path) if config_path else MailFlowConfig()

    logging_runtime: LoggingRuntime | None = None
    if enable_logging:
        logging_runtime = configure_logging(
            config.logging,
            secrets=[llm.api_key for llm in config.llms if llm.api_key],
            extra_handlers=extra_log_handlers,
            console_stream=output,
        )

    try:
        i18n = I18n(
            config.i18n.language or config.general.language,
            extra_dirs=config.i18n.extra_dirs,
        )
        manager = plugin_manager or PluginManager(config)
        if discover_plugins and plugin_manager is None:
            manager.discover()
        registry = manager.build_registry()

        storage = registry.storage_factory(config.storage.provider)(config.storage)

        sources = _build_sources(config, registry)
        backends, llm_configs = _build_llms(config, registry)
        router = LLMRouterImpl(backends, llm_configs)
        pipeline = _build_processors(config, registry, router)

        notifiers = [
            registry.notifier_factory(notifier.provider)(notifier)
            for notifier in config.notifiers
            if notifier.enabled
        ]
        notifier_configs = [n for n in config.notifiers if n.enabled]

        service = MailFlowService(
            config=config,
            registry=registry,
            plugin_manager=manager,
            storage=storage,
            sources=sources,
            router=router,
            pipeline=pipeline,
            notifiers=notifiers,
            notifier_configs=notifier_configs,
            events=EventBus(),
            i18n=i18n,
            logging_runtime=logging_runtime,
        )
        await service.start()
        return service
    except Exception:
        if logging_runtime is not None:
            logging_runtime.close()
        raise


def run_service(
    config: MailFlowConfig | None = None,
    config_path: str | Path | None = None,
    *,
    output: TextIO | None = None,
) -> None:
    """Standalone convenience wrapper: start the service and wait forever."""

    async def _run() -> None:
        service = await start_service(config, config_path, output=output)
        try:
            await service.wait()
        finally:
            await service.stop()

    from contextlib import suppress

    with suppress(KeyboardInterrupt):
        asyncio.run(_run())


__all__ = ["MailFlowService", "run_service", "start_service"]
