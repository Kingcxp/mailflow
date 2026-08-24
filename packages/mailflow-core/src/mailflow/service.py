"""The public MailFlow service facade.

One object exposes everything a CLI, TUI or chat-bot host needs: runtime
snapshots, mail/action/trash queries, urgency mutations, persistent language,
and the confirmed reply workflow. ``start_service()`` is the single entry
point that composes configuration, plugins, storage, LLMs, processors,
sources, notifiers, events, logging and the runtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TextIO, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from mailflow import __version__
from mailflow.config import (
    LLMConfig,
    MailFlowConfig,
    NotifierConfig,
    ProcessorConfig,
    load_config,
    patch_config_value,
    write_config,
)
from mailflow.contracts import (
    HistoryCapableSource,
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
    ComponentKind,
    LLMSnapshot,
    MailMessage,
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
from mailflow.letters import build_letter
from mailflow.llm import LLMRouterImpl
from mailflow.logging import LoggingRuntime, configure_logging
from mailflow.pipeline import PipelineEngine, build_bindings
from mailflow.plugins import PluginManager
from mailflow.processors import LLMImportanceProcessor as _BUILTIN_LLM_IMPORTANCE
from mailflow.processors import register_builtin_processors
from mailflow.registry import ComponentRegistry
from mailflow.runtime import MailFlowRuntime
from mailflow.settings import (
    OptionSpec,
    SettingsSection,
    add_entry,
    apply_value,
    build_sections,
    find_spec,
    move_entry,
    normalize_llm_chain,
    remove_entry,
    reset_value,
    update_entry,
)
from mailflow.updates import UpdateReport

logger = logging.getLogger("mailflow.service")

_LANGUAGE_PREFERENCE = "language"
_REPLY_TOKEN_TTL = timedelta(minutes=10)
_LIVE_GROUPS = frozenset({"accounts", "llms", "processors", "notifiers"})
"""Config groups whose changes hot-apply to the running runtime."""


def _bind_llm_processor(config: MailFlowConfig) -> MailFlowConfig:
    """Give the built-in LLM analysis a binding as soon as an LLM exists.

    Adding the first LLM (or renaming ids) must make analysis work without
    the user hand-writing a [[processors]] entry: bind to the first LLM in
    the chain — which is also the fallback head — and leave explicit user
    bindings untouched.
    """
    if not config.llms:
        return config
    llm_ids = [llm.llm_id for llm in config.llms]
    processor = next((p for p in config.processors if p.provider == "llm-importance"), None)
    if processor is None:
        config.processors.append(
            ProcessorConfig(
                processor_id="llm-importance",
                provider="llm-importance",
                priority=20,
                llm=llm_ids[0],
                fallback_llms=llm_ids[1:],
            )
        )
        return config
    if processor.llm is None or processor.llm not in llm_ids:
        processor.llm = llm_ids[0]
        processor.fallback_llms = llm_ids[1:]
    return config


class _DraftLocks:
    """One async lock per draft id, created on demand; bounds memory to the
    number of drafts confirmed concurrently during this service's life."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def for_draft(self, draft_id: str) -> asyncio.Lock:
        return self._locks.setdefault(draft_id, asyncio.Lock())


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
        self.config_path: Path | None = None
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
        from mailflow.plugin_market import PluginMarket, Repository

        self.market = PluginMarket(
            [Repository(repo.name, repo.url) for repo in config.plugins.repositories]
        )
        self._started = False
        self._stopped_event = asyncio.Event()
        self._stop_task: asyncio.Task[Any] | None = None
        self._update_task: asyncio.Task[Any] | None = None
        self.commands: Any | None = None  # CommandRouter wired by mailflow.commands
        self._reply_locks = _DraftLocks()

    # -- lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        await self.storage.initialize()
        await self._load_persisted_language()
        await self.runtime.start()
        self._started = True
        self._stopped_event = asyncio.Event()
        self._update_task = asyncio.create_task(self._update_loop(), name="updates")
        logger.info("mailflow service started (version %s)", __version__)

    async def stop(self) -> None:
        self._stopped_event.set()
        if self._update_task is not None:
            self._update_task.cancel()
        await self.runtime.stop()
        await self.storage.close()
        self._started = False
        logger.info("mailflow service stopped")

    async def reload_runtime(self) -> None:
        """Rebuild sources, LLMs, pipeline and notifiers from the current
        config without restarting: plugin enable/disable, account edits and
        notifier changes apply immediately (storage swaps still require a
        restart)."""
        registry = self.plugin_manager.build_registry()
        register_builtin_processors(registry)
        self.registry = registry
        sources = _build_sources(self.config, registry)
        backends, llm_configs = _build_llms(self.config, registry)
        router = LLMRouterImpl(backends, llm_configs)
        language = self.config.general.summary_language or self.i18n.language
        pipeline = _build_processors(self.config, registry, router, language=language)
        notifiers: list[Notifier] = []
        notifier_configs: list[NotifierConfig] = []
        for notifier in self.config.notifiers:
            if not notifier.enabled:
                continue
            if not registry.has(ComponentKind.NOTIFIER, notifier.provider):
                logger.warning(
                    "notifier %r: provider %r not loaded; skipping",
                    notifier.notifier_id,
                    notifier.provider,
                )
                continue
            notifiers.append(registry.notifier_factory(notifier.provider)(notifier))
            notifier_configs.append(notifier)
        self.router = router
        self.pipeline = pipeline
        self.sources = sources
        await self.runtime.reconfigure(
            config=self.config,
            sources=sources,
            pipeline=pipeline,
            notifiers=notifiers,
            notifier_configs=notifier_configs,
        )

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

    # -- mailbox history (browse already-received mail on demand) -------------

    def history_accounts(self) -> list[str]:
        """Account ids whose source adapter can list received mail."""
        return [
            account_id
            for account_id, source in self.sources.items()
            if isinstance(source, HistoryCapableSource)
        ]

    async def fetch_history(
        self, account_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[MailMessage]:
        """Newest-first window of mail already sitting in the account.

        Nothing is stored or analyzed: the caller picks which messages to run
        through the pipeline via :meth:`process_mail`. Raises ``KeyError``
        for an unknown account and ``NotImplementedError`` when the adapter
        has no history capability.
        """
        source = self.sources.get(account_id)
        if source is None:
            raise KeyError(f"no mail source for account {account_id!r}")
        if not isinstance(source, HistoryCapableSource):
            raise NotImplementedError(
                f"source for account {account_id!r} cannot list historical mail"
            )
        return await source.fetch_history(limit=limit, offset=offset)

    async def process_mail(self, mail: MailMessage, *, force: bool = False) -> MailRecord | None:
        """Analyze and store one mail immediately (same path as live mail).

        Returns the stored record, or ``None`` when it was already processed.
        With ``force=True`` an existing record is replaced by the fresh run —
        used when the user explicitly picks history mails for re-analysis."""
        return await self.runtime.process_mail_now(mail, force=force)

    async def is_mail_known(self, mail: MailMessage) -> bool:
        """True when this mail is already stored (so the UI can mark it)."""
        return await self.storage.get_mail(mail.normalized_message_id()) is not None

    async def list_actions(self) -> list[ActionItem]:
        """All timed action items: mail-derived plus user-created, by due time."""
        items: list[ActionItem] = []
        for record in await self.storage.list_mails():
            items.extend(record.action_items)
        items.extend(await self.storage.list_custom_actions())
        return sorted(items, key=lambda item: item.due_at)

    async def add_action(
        self,
        summary: str,
        due_at: datetime,
        *,
        action_type: str = "errand",
        notes: str = "",
    ) -> ActionItem:
        """Create a user-created timed action item (mail_id stays empty); it
        participates in the reminder scheduler like mail-derived items."""
        if not summary.strip():
            raise ValueError("action summary must not be empty")
        if due_at.tzinfo is None:
            raise ValueError("due_at must be timezone-aware")
        item = ActionItem(
            item_id=uuid4().hex,
            mail_id="",
            summary=summary.strip(),
            action_type=action_type.strip() or "errand",
            due_at=due_at,
            due_end=None,
            notes=notes.strip(),
        )
        await self.storage.save_custom_action(item)
        return item

    async def delete_action(self, item_id: str) -> bool:
        """Delete a user-created action item; mail-derived items are not
        stored here and are left untouched."""
        return await self.storage.delete_custom_action(item_id)

    # -- user feedback (filter tuning) -------------------------------------------

    async def record_feedback(self, mail_id: str, reason: str) -> None:
        """Store a user note on why a mail was irrelevant or unwanted.

        The note joins the rolling guidelines that are injected into every
        LLM analysis, so the model adjusts its filtering strategy with a
        grounded rationale (kept to the most recent entries).
        """
        if not reason.strip():
            raise ValueError("feedback reason must not be empty")
        await self.storage.set_preference(f"feedback.{mail_id}", reason.strip())
        guidelines = await self.storage.get_preference("feedback.guidelines") or ""
        lines = [line for line in guidelines.splitlines() if line.strip()][-19:]
        lines.append(f"{mail_id}: {reason.strip()}")
        await self.storage.set_preference("feedback.guidelines", "\n".join(lines))

    async def get_feedback(self, mail_id: str) -> str | None:
        return await self.storage.get_preference(f"feedback.{mail_id}")

    async def feedback_guidelines(self) -> str:
        return await self.storage.get_preference("feedback.guidelines") or ""

    # -- plugin update sources --------------------------------------------------

    async def record_plugin_source(self, plugin_id: str, source: str) -> None:
        """Remember where a plugin was installed from ('' clears it)."""
        if source:
            await self.storage.set_preference(f"plugin.source.{plugin_id}", source)
        else:
            await self.clear_plugin_source(plugin_id)

    async def _update_loop(self) -> None:
        """Daily auto-update: once per local day, check MailFlow releases and
        plugin versions and apply them (respects ``general.auto_update``)."""
        while not self._stopped_event.is_set():
            try:
                await self._run_daily_update()
            except Exception as exc:
                logger.error("daily update check failed: %s", exc)
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._stopped_event.wait(), timeout=3600)

    async def _run_daily_update(self) -> None:
        if not self.config.general.auto_update:
            return
        today = datetime.now(ZoneInfo(self.config.general.timezone)).date().isoformat()
        if await self.storage.get_preference(f"update.check.{today}"):
            return
        await self.storage.set_preference(f"update.check.{today}", "done")
        report = await self.check_updates()
        await self.events.emit(
            "mailflow.update.checked",
            mailflow_current=report.mailflow_current,
            mailflow_latest=report.mailflow_latest,
            plugin_updates={
                plugin_id: {"from": old, "to": new}
                for plugin_id, (old, new) in report.plugin_updates.items()
            },
        )
        if not report.has_updates:
            return
        results = await self.apply_updates()
        await self.events.emit("mailflow.update.applied", results=results)

    async def clear_plugin_source(self, plugin_id: str) -> None:
        await self.storage.set_preference(f"plugin.source.{plugin_id}", "")

    async def plugin_sources(self) -> dict[str, str]:
        """plugin_id -> recorded install source ('' when local/unknown)."""
        sources: dict[str, str] = {}
        for info in self.plugin_manager.enabled_infos():
            sources[info.plugin_id] = (
                await self.storage.get_preference(f"plugin.source.{info.plugin_id}") or ""
            )
        return sources

    async def check_updates(self) -> UpdateReport:
        from mailflow.updates import check_updates

        return await asyncio.to_thread(
            check_updates,
            self.market,
            installed_plugins=await self.installed_plugin_versions(),
            sources=await self.plugin_sources(),
            mailflow_current=__version__,
        )

    async def installed_plugin_versions(self) -> dict[str, str]:
        from mailflow.updates import installed_plugin_versions

        return await asyncio.to_thread(installed_plugin_versions)

    async def apply_updates(self) -> dict[str, str]:
        """Apply every available update; returns plugin_id -> outcome and
        reports the mailflow upgrade result under a ``mailflow`` key."""
        from mailflow.updates import apply_plugin_updates, upgrade_mailflow

        report = await self.check_updates()
        results: dict[str, str] = {}
        if report.mailflow_update:
            try:
                results["mailflow"] = await upgrade_mailflow()
            except Exception as exc:
                logger.error("mailflow upgrade failed: %s", exc)
                results["mailflow"] = f"failed: {exc}"
        if report.plugin_updates:
            results.update(await apply_plugin_updates(self.market, report.plugin_updates))
        return results

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

    async def purge_all_processed(self) -> tuple[int, int]:
        """Wipe every active mail and the entire trash, then forget in-memory
        dedup state — the "start over" action for wrong historical analyses.

        Live polling is unaffected (new mail keeps arriving); mails still
        sitting in an open history browser can be force re-analyzed back into
        storage via ``process_mail(force=True)``.
        """
        moved, purged = await self.run_cleanup_wide()
        self.runtime.reset_dedup()
        return moved, purged

    async def run_cleanup_wide(self) -> tuple[int, int]:
        """Move every active mail to trash and purge the trash permanently."""
        from mailflow.domain import utcnow

        horizon_active = utcnow() + timedelta(days=1)
        horizon_trash = utcnow() + timedelta(days=36500)
        moved = await self.storage.cleanup_mail(horizon_active)
        purged = await self.storage.purge_trash(horizon_trash)
        return moved, purged

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

    # -- configuration inspection and mutation ------------------------------------

    def list_config_options(self) -> list[Any]:
        from mailflow.config import inspect_config

        return inspect_config(self.config)

    def get_config_option(self, key: str) -> Any:
        from mailflow.config import find_option

        option = find_option(self.config, key)
        if option is None:
            raise KeyError(f"unknown config option {key!r}")
        return option

    async def set_config_value(self, key: str, raw_value: str) -> Any:
        """Coerce, validate and persist one scalar config option."""
        from mailflow.config import patch_config_value, set_option_value, write_config

        if self.config_path is None:
            raise ValueError("no config file loaded; start with --config to persist changes")
        updated = set_option_value(self.config, key, raw_value)
        patched = patch_config_value(self.config_path, key, _config_value_of(updated, key))
        if not patched:
            write_config(updated, self.config_path)
        self.config = updated
        await self.events.emit("config.changed", key=key)
        return self.get_config_option(key)

    # -- settings editor (sections, typed edits, list entries) -----------------

    def _settings_context(self) -> dict[str, Any]:
        """Registry, plugin titles and language codes for the editor model:
        options land in their owner's section and general.language becomes
        a dropdown of the loaded packs."""
        return {
            "registry": self.registry,
            "language_choices": tuple(self.i18n.available_codes()),
        }

    def settings_sections(self) -> list[SettingsSection]:
        """Sidebar model: MailFlow's own sections plus one per owning plugin."""
        return build_sections(self.config, **self._settings_context())

    def settings_option(self, key: str) -> OptionSpec | None:
        return find_spec(self.config, key, **self._settings_context())

    async def _persist_config(self, updated: MailFlowConfig, key: str) -> None:
        """Write ``updated`` back, preferring a comment-preserving patch."""
        if self.config_path is None:
            raise ValueError("no config file loaded; start with --config to persist changes")
        patched = False
        if "[" not in key and key.count(".") == 1:
            with contextlib.suppress(AttributeError, KeyError):
                patched = patch_config_value(self.config_path, key, _config_value_of(updated, key))
        if not patched:
            write_config(updated, self.config_path)
        self.config = updated
        await self.events.emit("config.changed", key=key)
        if key.split(".")[0] in _LIVE_GROUPS:
            # accounts/llms/processors/notifiers changes apply immediately;
            # a runtime rebuild problem must never swallow the fact that the
            # config itself was persisted (panes re-read it regardless)
            try:
                await self.reload_runtime()
            except Exception as exc:
                logger.error("hot reload failed after %s change: %s", key, exc)

    async def set_setting(self, key: str, raw_value: Any) -> OptionSpec | None:
        """Coerce, validate and persist one option (scalar, list or mapping).

        Raises :class:`mailflow.settings.SettingsError` naming the offending
        option when the value is invalid, so a host can point at the field.
        """
        if key == "general.language":
            return await self._set_language_setting(raw_value)
        updated = apply_value(self.config, key, raw_value, **self._settings_context())
        await self._persist_config(updated, key)
        return self.settings_option(key)

    async def _set_language_setting(self, raw_value: Any) -> OptionSpec | None:
        """Validate, switch the running UI and persist the interface language.

        Works with or without a config file: embedded hosts (no ``--config``)
        still get a live switch through the stored preference.
        """
        code = str(raw_value).strip()
        updated = apply_value(
            self.config, "general.language", code, **self._settings_context()
        )  # SettingsError for an unloaded pack (choice validation)
        if self.config_path is None:
            self.config = updated
            await self.events.emit("config.changed", key="general.language")
        else:
            await self._persist_config(updated, "general.language")
        if code in self.i18n.available_codes() and code != self.i18n.language:
            self.i18n.set_language(code)
            await self.storage.set_preference(_LANGUAGE_PREFERENCE, code)
            await self.events.emit("language.changed", language=code)
        return self.settings_option("general.language")

    async def reset_setting(self, key: str) -> OptionSpec | None:
        """Restore one option to its schema default and persist."""
        updated = reset_value(self.config, key, **self._settings_context())
        await self._persist_config(updated, key)
        return self.settings_option(key)

    async def add_config_entry(self, group: str, values: dict[str, Any]) -> MailFlowConfig:
        """Append a validated entry to accounts/llms/processors/notifiers."""
        updated = add_entry(self.config, group, values)
        if group == "llms":
            updated = normalize_llm_chain(updated)
            updated = _bind_llm_processor(updated)
        await self._persist_config(updated, group)
        return updated

    async def update_config_entry(
        self, group: str, index: int, values: dict[str, Any]
    ) -> MailFlowConfig:
        updated = update_entry(self.config, group, index, values)
        if group == "llms":
            updated = normalize_llm_chain(updated)
            updated = _bind_llm_processor(updated)
        await self._persist_config(updated, group)
        return updated

    async def remove_config_entry(self, group: str, index: int) -> MailFlowConfig:
        updated = remove_entry(self.config, group, index)
        if group == "llms":
            updated = normalize_llm_chain(updated)
        await self._persist_config(updated, group)
        return updated

    async def move_config_entry(self, group: str, index: int, offset: int) -> MailFlowConfig:
        """Reorder one entry; for LLMs the order *is* the fallback chain."""
        updated = move_entry(self.config, group, index, offset)
        await self._persist_config(updated, group)
        return updated

    # -- plugin marketplace ------------------------------------------------------------

    async def plugin_repo_add(self, name: str, url: str) -> None:
        """Register a marketplace repository (persisted to the config file)."""
        if self.config_path is None:
            raise ValueError("no config file loaded; start with --config to persist changes")
        from mailflow.config import PluginRepositoryConfig, write_config

        repos = list(self.config.plugins.repositories)
        if any(repo.name == name for repo in repos):
            raise ValueError(f"repository {name!r} already configured")
        repos.append(PluginRepositoryConfig(name=name, url=url))
        self.config.plugins.repositories = repos
        write_config(self.config, self.config_path)
        from mailflow.plugin_market import PluginMarket, Repository

        self.market = PluginMarket([Repository(r.name, r.url) for r in repos])

    async def plugin_repo_remove(self, name: str) -> None:
        if self.config_path is None:
            raise ValueError("no config file loaded; start with --config to persist changes")
        from mailflow.config import write_config

        repos = list(self.config.plugins.repositories)
        remaining = [repo for repo in repos if repo.name != name]
        if len(remaining) == len(repos):
            raise KeyError(f"repository {name!r} not configured")
        self.config.plugins.repositories = remaining
        write_config(self.config, self.config_path)
        from mailflow.plugin_market import PluginMarket, Repository

        self.market = PluginMarket([Repository(r.name, r.url) for r in remaining])

    # -- plugin lifecycle: enable / disable / uninstall -----------------------------

    async def _require_known_plugin(self, plugin_id: str) -> None:
        """The plugin must be loaded (registry) or installed (entry point/package)."""
        from mailflow.plugin_market import PluginMarket

        loaded = {p.plugin_id for p in self.plugin_manager.enabled_infos()}
        if plugin_id in loaded:
            return
        if PluginMarket.is_installed(plugin_id):
            return
        # bundled plugins: the distribution package name equals the plugin id
        if PluginMarket.is_installed(plugin_id, package=plugin_id):
            return
        try:
            found = await asyncio.to_thread(self.market.find, plugin_id)
        except OSError as exc:  # URLError/timeout: marketplace unreachable
            logger.warning("marketplace lookup for %r failed: %s", plugin_id, exc)
            found = None
        if found is not None and PluginMarket.is_installed(found[1].id, package=found[1].package):
            return
        raise KeyError(self.t("plugin.unknown_plugin", plugin_id=plugin_id))

    async def plugin_disable(self, plugin_id: str) -> None:
        """Disable a plugin; its components unload immediately (config
        entries stay, so re-enabling restores them)."""
        if self.config_path is None:
            raise ValueError("no config file loaded; start with --config to persist changes")
        from mailflow.config import write_config

        await self._require_known_plugin(plugin_id)
        plugins = self.config.plugins
        if plugin_id not in plugins.disabled:
            plugins.disabled.append(plugin_id)
        plugins.enabled = [p for p in plugins.enabled if p != plugin_id]
        write_config(self.config, self.config_path)
        await self.events.emit("plugin.disabled", plugin_id=plugin_id)
        await self.reload_runtime()

    def _auto_instances_for(self, plugin_id: str) -> int:
        """Ensure every notifier component of ``plugin_id`` has at least one
        [[notifiers]] instance so enabling it has an observable effect;
        returns how many instances were created. Sources and LLM backends
        need credentials and are deliberately left to the user."""
        created = 0
        for component in self.registry.snapshots():
            if component.plugin_id != plugin_id:
                continue
            if component.kind != ComponentKind.NOTIFIER:
                continue
            if any(n.provider == component.component_id for n in self.config.notifiers):
                continue
            self.config.notifiers.append(
                NotifierConfig(
                    notifier_id=f"{component.component_id}", provider=component.component_id
                )
            )
            created += 1
        return created

    async def plugin_enable(self, plugin_id: str) -> str:
        """Enable a plugin: components load immediately and notifier plugins
        get a default instance when none exists. Returns the id of the
        auto-created instance ('' when none was needed)."""
        if self.config_path is None:
            raise ValueError("no config file loaded; start with --config to persist changes")
        from mailflow.config import write_config

        await self._require_known_plugin(plugin_id)
        plugins = self.config.plugins
        plugins.disabled = [p for p in plugins.disabled if p != plugin_id]
        # an explicit `enabled` list acts as an allowlist: make sure the
        # plugin is part of it
        if plugins.enabled and plugin_id not in plugins.enabled:
            plugins.enabled.append(plugin_id)
        created_instance = ""
        if self._auto_instances_for(plugin_id):
            created_instance = f"{self.config.notifiers[-1].notifier_id}"
        write_config(self.config, self.config_path)
        await self.events.emit("plugin.enabled", plugin_id=plugin_id)
        await self.reload_runtime()
        return created_instance

    def plugin_status(self, plugin_id: str) -> str:
        """'enabled' | 'disabled' | 'not_loaded' for the given plugin id."""
        plugins = self.config.plugins
        if plugin_id in plugins.disabled:
            return "disabled"
        if plugins.enabled and plugin_id not in plugins.enabled:
            return "disabled"
        loaded = {p.plugin_id for p in self.plugin_manager.enabled_infos()}
        return "enabled" if plugin_id in loaded else "not_loaded"

    async def plugin_uninstall(self, plugin_id: str) -> str:
        """Uninstall a marketplace plugin (uv pip uninstall of its package)."""
        from mailflow.plugin_market import PluginMarket

        found = await asyncio.to_thread(self.market.find, plugin_id)
        plugin = found[1] if found else None
        if plugin is None:
            raise KeyError(f"plugin {plugin_id!r} not found in any repository")
        if not PluginMarket.is_installed(plugin_id, package=plugin.package):
            return f"{plugin_id} is not installed"
        return await self.market.uninstall(plugin)

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

    async def create_letter_draft(
        self,
        mail_id: str,
        language: str,
        *,
        opening: str = "",
        body: str = "",
        signature: str = "",
    ) -> ReplyDraft:
        """Create a reply draft pre-filled with a formal letter template
        (``"cn"`` or ``"en"``): the date is filled automatically and the
        signature block is right-aligned. Empty parts keep placeholders for
        the user to fill in."""
        record = await self.storage.get_mail(mail_id)
        if record is None:
            raise KeyError(f"mail {mail_id} not found")
        tz = ZoneInfo(self.config.general.timezone)
        today = datetime.now(tz).date()
        recipient = record.mail.sender.display or record.mail.sender.address
        body_html = build_letter(
            language,
            recipient=recipient,
            today=today,
            opening=opening,
            body=body,
            signature=signature,
        )
        draft = ReplyDraft(
            draft_id=uuid4().hex[:16],
            mail_id=mail_id,
            account_id=record.mail.account_id,
            to=record.mail.sender,
            subject=f"Re: {record.mail.subject}",
            body=body_html,
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
        # Two concurrent confirms with the same token must not both pass the
        # validity check before either persists SENT (a double send). The
        # per-draft lock serializes claim → re-check → send.
        async with self._reply_locks.for_draft(draft_id):
            return await self._confirm_reply_locked(draft_id, token)

    async def _confirm_reply_locked(self, draft_id: str, token: str) -> ReplyDraft:
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
        if not registry.has(ComponentKind.MAIL_SOURCE, account.provider):
            logger.warning(
                "account %r: source adapter %r not loaded (disabled or uninstalled); skipping",
                account.account_id,
                account.provider,
            )
            continue
        factory = registry.source_factory(account.provider)
        sources[account.account_id] = factory(account)
    return sources


def _build_llms(
    config: MailFlowConfig, registry: ComponentRegistry
) -> tuple[dict[str, LLMBackend], dict[str, LLMConfig]]:
    backends: dict[str, LLMBackend] = {}
    for llm_config in config.llms:
        if not registry.has(ComponentKind.LLM_BACKEND, llm_config.provider):
            logger.warning(
                "llm %r: backend %r not loaded (disabled or uninstalled); skipping",
                llm_config.llm_id,
                llm_config.provider,
            )
            continue
        factory = registry.llm_factory(llm_config.provider)
        backends[llm_config.llm_id] = factory(llm_config)
    configs = {llm.llm_id: llm for llm in config.llms}
    return backends, configs


def _default_processors() -> list[ProcessorConfig]:
    """No processors run out of the box: without a configured LLM there is
    no meaningful analysis (mails are stored with the subject as summary),
    and the keyword pre-filter produced low-quality canned summaries.
    ``_bind_llm_processor`` creates the LLM binding automatically as soon
    as the first LLM is configured; users can still add ``rules`` or any
    other processor explicitly."""
    return []


def _build_llm_enhancers(config: MailFlowConfig, registry: ComponentRegistry) -> list[Any]:
    """Instantiate every registered LLM enhancer with its config section."""
    from mailflow.contracts import LLMEnhancer

    enhancers: list[LLMEnhancer] = []
    for enhancer_id in registry.component_ids(ComponentKind.LLM_ENHANCER):
        factory = registry.llm_enhancer_factory(enhancer_id)
        enhancer_config = next(
            (
                section
                for section in config.processors
                if section.provider == enhancer_id or section.processor_id == enhancer_id
            ),
            None,
        )
        # An explicit section may disable the enhancer; without a section
        # the enhancer is active (installing a plugin enables it).
        if enhancer_config is not None and not enhancer_config.enabled:
            continue
        if enhancer_config is None:
            enhancer_config = ProcessorConfig(processor_id=enhancer_id, provider=enhancer_id)
        enhancers.append(cast(Any, factory(enhancer_config)))
    return enhancers


def _build_processors(
    config: MailFlowConfig,
    registry: ComponentRegistry,
    router: LLMRouter,
    *,
    language: str = "",
) -> PipelineEngine:
    processor_configs: list[ProcessorConfig] = []
    processors: dict[str, MailProcessor] = {}
    plugin_of: dict[str, str] = {}
    enhancers = _build_llm_enhancers(config, registry)
    for processor_config in config.processors or _default_processors():
        if not processor_config.enabled:
            continue
        if not registry.has(ComponentKind.MAIL_PROCESSOR, processor_config.provider):
            logger.warning(
                "processor %r: provider %r not loaded (disabled or uninstalled); skipping",
                processor_config.processor_id,
                processor_config.provider,
            )
            continue
        factory = registry.processor_factory(processor_config.provider)
        if (
            processor_config.provider == "llm-importance"
            and not processor_config.options.get("language")
            and (language or "").strip()
        ):
            # Per-mail summary language: explicit config option wins, else
            # the configured general.summary_language or the UI language.
            processor_config = processor_config.model_copy(
                update={
                    "options": {
                        **processor_config.options,
                        "language": language,
                    }
                }
            )
        if (
            processor_config.provider == "llm-importance"
            and enhancers
            and factory is _BUILTIN_LLM_IMPORTANCE
        ):
            # The built-in factory takes the enhancer list as a third
            # argument; a plugin-replaced factory keeps the 2-arg contract.
            processors[processor_config.processor_id] = cast(Any, factory)(
                processor_config, router, enhancers
            )
        else:
            processors[processor_config.processor_id] = factory(processor_config, router)
        plugin_of[processor_config.processor_id] = (
            registry.plugin_for(processor_config.provider) or ""
        )
        processor_configs.append(processor_config)
    bindings = build_bindings(processor_configs, processors, plugin_of)
    return PipelineEngine(bindings, router=router)


def _config_value_of(config: MailFlowConfig, key: str) -> Any:
    """Current value at a dotted key path (for comment-preserving patches)."""
    node: Any = config
    for part in key.split("."):
        node = getattr(node, part)
    return node


def _collect_secrets(config: MailFlowConfig) -> list[str]:
    """API keys plus header values that look like tokens (defense in depth)."""
    secrets: list[str] = []
    token_markers = ("key", "token", "auth", "bearer", "secret")
    for llm in config.llms:
        if llm.api_key:
            secrets.append(llm.api_key)
        for name, value in llm.headers.items():
            lowered = name.lower()
            if any(marker in lowered for marker in token_markers) and len(value) >= 8:
                secrets.append(value)
    return secrets


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
            secrets=_collect_secrets(config),
            extra_handlers=extra_log_handlers,
            console_stream=output,
        )

    try:
        i18n = I18n(
            config.i18n.language or config.general.language,
            extra_dirs=config.i18n.extra_dirs,
        )
        language = config.general.summary_language or i18n.language
        manager = plugin_manager or PluginManager(config)
        if discover_plugins and plugin_manager is None:
            manager.discover()
        registry = manager.build_registry()
        register_builtin_processors(registry)

        storage = registry.storage_factory(config.storage.provider)(config.storage)

        sources = _build_sources(config, registry)
        backends, llm_configs = _build_llms(config, registry)
        router = LLMRouterImpl(backends, llm_configs)
        pipeline = _build_processors(config, registry, router, language=language)

        notifiers: list[Notifier] = []
        notifier_configs: list[NotifierConfig] = []
        for notifier in config.notifiers:
            if not notifier.enabled:
                continue
            if not registry.has(ComponentKind.NOTIFIER, notifier.provider):
                logger.warning(
                    "notifier %r: provider %r not loaded (disabled or uninstalled); skipping",
                    notifier.notifier_id,
                    notifier.provider,
                )
                continue
            notifiers.append(registry.notifier_factory(notifier.provider)(notifier))
            notifier_configs.append(notifier)

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
        if config_path is not None:
            service.config_path = Path(config_path)
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
