"""Asynchronous runtime: merges source adapters into a bounded stream,
runs the processing pipeline with configurable workers, applies notifier
thresholds, isolates per-account failures and schedules the daily retention
cleanup (default 04:00 local time).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import Any, cast
from zoneinfo import ZoneInfo

from mailflow.config import GeneralConfig, MailAccountConfig, MailFlowConfig, NotifierConfig
from mailflow.contracts import MailSource, Notifier, StorageBackend
from mailflow.domain import ActionItem, MailMessage, MailRecord, to_utc
from mailflow.events import EventBus
from mailflow.pipeline import PipelineEngine

logger = logging.getLogger("mailflow.runtime")
reminder_logger = logging.getLogger("mailflow.reminder")

_WAIT_TIMEOUT = 0.5  # seconds between queue polls while stopping
_EVENT_PREFIX = "mailflow."


def seconds_until_next_cleanup(
    now: datetime, timezone_name: str, hour: int, minute: int = 0
) -> float:
    """Seconds from ``now`` until the next daily ``hour:minute`` in the zone."""
    tz = ZoneInfo(timezone_name)
    local_now = now.astimezone(tz)
    target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local_now:
        target += timedelta(days=1)
    return (target - local_now).total_seconds()


def reminder_times(
    item: ActionItem,
    timezone_name: str,
    days_before: int,
    hour: int,
    minute: int,
) -> list[tuple[datetime, str]]:
    """The two reminder windows for an action item, in the configured zone.

    Returns ``(local_datetime, kind)`` pairs: the early reminder
    (``days_before`` days before the due date at ``hour:minute``) and the
    day-of reminder (00:00 on the due date).
    """
    tz = ZoneInfo(timezone_name)
    due = to_utc(item.due_at).astimezone(tz)
    early_date = due.date() - timedelta(days=days_before)
    early = datetime.combine(early_date, dt_time(hour, minute), tzinfo=tz)
    midnight = datetime.combine(due.date(), dt_time(0, 0), tzinfo=tz)
    return [(early, "early"), (midnight, "day_of")]


class MailFlowRuntime:
    """Owns source tasks, pipeline workers and the cleanup scheduler."""

    def __init__(
        self,
        config: MailFlowConfig,
        *,
        sources: dict[str, MailSource],  # keyed by account_id
        pipeline: PipelineEngine,
        storage: StorageBackend,
        notifiers: list[Notifier],
        notifier_configs: list[NotifierConfig],
        events: EventBus,
        account_configs: list[MailAccountConfig],
        i18n: Any = None,
    ) -> None:
        self._config = config
        self._sources = dict(sources)
        self._pipeline = pipeline
        self._storage = storage
        self._notifiers = list(notifiers)
        self._notifier_configs = list(notifier_configs)
        self._events = events
        self._account_configs = list(account_configs)
        # i18n for user-visible reminder/digest log lines; None keeps the
        # English fallback (standalone test harnesses)
        self._i18n = i18n

        self._queue: asyncio.Queue[MailMessage] = asyncio.Queue(maxsize=config.general.queue_size)
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self._account_status: dict[str, str] = {}
        self._account_errors: dict[str, str | None] = {}
        self._started_at: datetime | None = None
        self._closed = False
        # record ids seen since start; makes the dedup check atomic across
        # workers (storage lookup alone would race). Cross-restart dedup is
        # covered by the storage lookup in _process_one.
        self._seen_ids: set[str] = set()
        # account_id -> persisted UID watermark (read at start, written by
        # the injected store); lets IMAP polling resume across restarts.
        self._stored_watermarks: dict[str, str] = {}

    # -- lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        self._started_at = datetime.now()
        loop = asyncio.get_running_loop()
        # preload persisted IMAP watermarks so source factories can resume
        # polling where the previous run left off
        for account in self._account_configs:
            try:
                raw = await self._storage.get_preference(f"imap.watermark.{account.account_id}")
            except Exception:
                raw = None
            if raw is not None:
                self._stored_watermarks[account.account_id] = str(raw)
        for account in self._account_configs:
            if not account.enabled:
                self._account_status[account.account_id] = "stopped"
                continue
            self._account_status[account.account_id] = "starting"
            source = self._sources.get(account.account_id)
            if source is None:
                self._account_status[account.account_id] = "error"
                self._account_errors[account.account_id] = (
                    f"no source adapter for provider {account.provider!r}"
                )
                logger.error(
                    "account %r: no source adapter for provider %r",
                    account.account_id,
                    account.provider,
                )
                continue
            self._inject_watermark_store(source, account.account_id, loop)
            self._tasks.append(
                asyncio.create_task(
                    self._run_source(account, source), name=f"source-{account.account_id}"
                )
            )
        for index in range(self._config.general.workers):
            self._tasks.append(asyncio.create_task(self._worker(index), name=f"worker-{index}"))
        self._tasks.append(asyncio.create_task(self._cleanup_loop(), name="cleanup"))
        self._tasks.append(asyncio.create_task(self._reminder_loop(), name="reminders"))
        logger.info(
            "runtime started: %d accounts, %d workers, retention %d days",
            len(self._account_configs),
            self._config.general.workers,
            self._config.general.mail_retention_days,
        )

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        await self._events.emit(f"{_EVENT_PREFIX}runtime.stopping")
        if self._tasks:
            _done, pending = await asyncio.wait(self._tasks, timeout=10)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        for source in self._sources.values():
            try:
                await source.close()
            except Exception as exc:
                logger.warning("source close failed: %s", exc)
        for account_id in self._account_status:
            if self._account_status[account_id] == "running":
                self._account_status[account_id] = "stopped"
        logger.info("runtime stopped")

    async def reconfigure(
        self,
        *,
        config: MailFlowConfig,
        sources: dict[str, MailSource],
        pipeline: PipelineEngine,
        notifiers: list[Notifier],
        notifier_configs: list[NotifierConfig],
    ) -> None:
        """Hot-swap components after a settings change; the queue, workers
        and scheduler loops keep running. Source tasks are restarted (their
        adapters hold connections and per-adapter state); a restarted IMAP
        source resumes from its persisted watermark semantics.

        Storage swaps are deliberately not supported: a storage change still
        requires a restart.
        """
        old_source_tasks = [task for task in self._tasks if task.get_name().startswith("source-")]
        for task in old_source_tasks:
            task.cancel()
        if old_source_tasks:
            # A source parked in a blocking connect (to_thread cannot be
            # interrupted) must not stall a settings change indefinitely:
            # wait briefly, then proceed — the cancelled tasks finish in the
            # background and are already removed from the task list.
            await asyncio.wait(old_source_tasks, timeout=5.0)
        self._tasks = [task for task in self._tasks if task not in old_source_tasks]
        for source in self._sources.values():
            with suppress(Exception):
                await source.close()
        self._config = config
        self._account_configs = list(config.accounts)
        self._sources = dict(sources)
        self._pipeline = pipeline
        self._notifiers = list(notifiers)
        self._notifier_configs = list(notifier_configs)
        for account in self._account_configs:
            self._account_errors.pop(account.account_id, None)
            if not account.enabled:
                self._account_status[account.account_id] = "stopped"
                continue
            adapter = self._sources.get(account.account_id)
            if adapter is None:
                self._account_status[account.account_id] = "error"
                self._account_errors[account.account_id] = (
                    f"no source adapter for provider {account.provider!r}"
                )
                logger.error(
                    "account %r: no source adapter for provider %r",
                    account.account_id,
                    account.provider,
                )
                continue
            self._account_status[account.account_id] = "starting"
            self._inject_watermark_store(adapter, account.account_id, asyncio.get_running_loop())
            self._tasks.append(
                asyncio.create_task(
                    self._run_source(account, adapter), name=f"source-{account.account_id}"
                )
            )
        await self._events.emit(f"{_EVENT_PREFIX}runtime.reconfigured")
        logger.info("runtime reconfigured: %d accounts", len(self._account_configs))

    # -- source tasks ------------------------------------------------------------

    def _inject_watermark_store(self, source: MailSource, account_id: str, loop: Any) -> None:
        """Give a source (duck-typed) a persistent UID watermark backed by
        storage preferences, so a restart resumes where polling left off
        instead of re-seeding at the newest UID and silently skipping every
        mail that arrived while the service was down.

        Sources without ``set_watermark_store`` are untouched (the fake
        source has no UIDs to persist).
        """
        inject = getattr(source, "set_watermark_store", None)
        if inject is None:
            return
        key = f"imap.watermark.{account_id}"

        def load() -> int | None:
            raw = self._stored_watermarks.get(account_id)
            if raw is None:
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None

        def save(uid: int) -> None:
            self._stored_watermarks[account_id] = str(uid)
            asyncio.run_coroutine_threadsafe(self._storage.set_preference(key, str(uid)), loop)

        inject(load, save)

    # -- source tasks ------------------------------------------------------------

    async def _run_source(self, account: MailAccountConfig, source: MailSource) -> None:
        async def emit(mail: MailMessage) -> None:
            await self._events.emit(f"{_EVENT_PREFIX}mail.received", mail=mail)
            await self._queue.put(mail)

        try:
            self._account_status[account.account_id] = "running"
            await source.run(emit, self._stop_event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._account_status[account.account_id] = "error"
            self._account_errors[account.account_id] = str(exc)
            logger.error("source for account %r failed: %s", account.account_id, exc)
            await self._events.emit(
                f"{_EVENT_PREFIX}account.error",
                account_id=account.account_id,
                error=str(exc),
            )
        finally:
            if self._account_status.get(account.account_id) == "running":
                self._account_status[account.account_id] = "stopped"

    # -- workers -------------------------------------------------------------------

    async def _worker(self, index: int) -> None:
        logger.debug("worker %d started", index)
        while not self._stop_event.is_set():
            try:
                mail = await asyncio.wait_for(self._queue.get(), timeout=_WAIT_TIMEOUT)
            except TimeoutError:
                continue
            try:
                await self._process_one(mail)
            except Exception as exc:
                logger.error("failed to process mail %r: %s", mail.message_id, exc)
            finally:
                self._queue.task_done()

    def reset_dedup(self) -> None:
        """Forget in-memory dedup state so wiped mails can be re-ingested."""
        self._seen_ids.clear()

    async def process_mail_now(
        self, mail: MailMessage, *, force: bool = False
    ) -> MailRecord | None:
        """Run one mail through the pipeline immediately, bypassing the queue.

        Used by on-demand flows (the TUI mailbox browser) so a user-selected
        historical mail follows exactly the same path as a streamed one:
        dedup, persistence, ``mail.processed`` and notifier thresholds.
        Returns the stored record, or ``None`` when it was a duplicate.
        With ``force=True`` (explicit user re-analysis) the dedup checks are
        bypassed and the stored record is replaced with the fresh analysis.
        """
        if force:
            record_id = mail.normalized_message_id()
            self._seen_ids.discard(record_id)
            # _process_one emits mailflow.mail.processed on success; emitting
            # again here would double-notify every re-analysis.
            return await self._process_one(mail, _skip_dedup=True)
        return await self._process_one(mail)

    async def _process_one(
        self, mail: MailMessage, *, _skip_dedup: bool = False
    ) -> MailRecord | None:
        record_id = mail.normalized_message_id()
        if not _skip_dedup and record_id in self._seen_ids:
            logger.debug("duplicate mail skipped (already processed): %s", record_id)
            return None
        self._seen_ids.add(record_id)  # no await: atomic across workers
        if not _skip_dedup and await self._storage.get_mail(record_id) is not None:
            # Forwarded copies of the same mail (multiple accounts, restarts)
            # share the normalized id; process and store exactly one copy.
            logger.debug("duplicate mail skipped (already stored): %s", record_id)
            return None
        try:
            analysis, notes, _, _ = await self._pipeline.process(
                mail,
                mail.account_id,
                timezone=self._config.general.timezone,
                feedback_guidelines=(
                    await self._storage.get_preference("feedback.guidelines") or ""
                ),
            )
            record = MailRecord(
                record_id=record_id,
                mail=mail,
                auto_urgency=analysis.urgency,
                analysis=analysis,
                processor_notes=notes,
                received_at=mail.received_at,
            )
            await self._persist_with_retry(record)
        except Exception:
            # Not stored: release the dedup mark so a retry this session is
            # possible instead of silently dropping the mail forever.
            self._seen_ids.discard(record_id)
            raise
        await self._events.emit(f"{_EVENT_PREFIX}mail.processed", record=record)
        await self._notify(record)
        return record

    async def _persist_with_retry(self, record: MailRecord, attempts: int = 3) -> None:
        """Persist the record; a mail must not be lost to a transient storage error."""
        for attempt in range(attempts):
            try:
                await self._storage.save_mail(record)
                return
            except Exception as exc:
                if attempt >= attempts - 1:
                    logger.critical(
                        "mail %r could not be persisted after %d attempts: %s",
                        record.record_id,
                        attempts,
                        exc,
                    )
                    raise
                logger.error(
                    "save attempt %d/%d failed for %r: %s",
                    attempt + 1,
                    attempts,
                    record.record_id,
                    exc,
                )
                await asyncio.sleep(0.5 * (attempt + 1))

    async def _notify(self, record: MailRecord) -> None:
        for notifier, notifier_config in zip(self._notifiers, self._notifier_configs, strict=True):
            if record.effective_urgency.rank < notifier_config.minimum_urgency.rank:
                continue
            # a notification that silently disappears defeats the whole
            # point of the urgency contract: retry transient failures
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    await notifier.notify(record)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt < 2:
                        await asyncio.sleep(2.0 * (attempt + 1))
            if last_exc is not None:
                logger.warning(
                    "notifier %r failed for %r after 3 attempts: %s",
                    getattr(notifier, "backend_id", type(notifier).__name__),
                    record.record_id,
                    last_exc,
                )

    # -- retention cleanup ----------------------------------------------------------

    async def run_cleanup(self) -> None:
        now = datetime.now()
        retention_before = now - timedelta(days=self._config.general.mail_retention_days)
        trash_before = now - timedelta(days=self._config.general.trash_retention_days)
        moved = await self._storage.cleanup_mail(retention_before)
        purged = await self._storage.purge_trash(trash_before)
        logger.info(
            "cleanup ran: %d old mails moved to trash, %d trash records purged",
            moved,
            purged,
        )
        await self._events.emit(f"{_EVENT_PREFIX}cleanup.done", moved=moved, purged=purged)

    async def _cleanup_loop(self) -> None:
        while not self._stop_event.is_set():
            wait_seconds = seconds_until_next_cleanup(
                datetime.now(),
                self._config.general.timezone,
                self._config.general.cleanup_hour,
                self._config.general.cleanup_minute,
            )
            logger.debug("next cleanup in %.0f seconds", wait_seconds)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=max(wait_seconds, 1.0))
            if self._stop_event.is_set():
                break
            try:
                await self.run_cleanup()
            except Exception as exc:
                logger.error("cleanup run failed: %s", exc)

    async def _reminder_loop(self) -> None:
        interval = max(10, self._config.general.reminder_interval_seconds)
        while not self._stop_event.is_set():
            try:
                await self.run_reminder_tick()
            except Exception as exc:
                logger.error("reminder tick failed: %s", exc)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)

    async def run_reminder_tick(self) -> int:
        """Fire due action-item reminders; returns how many fired.

        A reminder fires once (persisted in preferences) once its scheduled
        time has passed — including when the app was off at that moment.
        """
        now = datetime.now(UTC)
        fired = 0
        config = self._config.general
        for record in await self._storage.list_mails():
            for item in record.action_items:
                fired += await self._fire_reminder(item, record, now, config)
        for item in await self._storage.list_custom_actions():
            fired += await self._fire_reminder(item, None, now, config)
        await self._fire_daily_digest(now, config)
        await self._fire_hourly_summary(now, config)
        return fired

    async def _fire_daily_digest(self, now: datetime, config: GeneralConfig) -> int:
        """Once per day, after the configured reminder hour, emit a digest of
        approaching actions: how many are due today and how many are coming
        up, plus the items themselves — the host paginates them into chat
        messages."""
        local_now = now.astimezone(ZoneInfo(config.timezone))
        if (local_now.hour, local_now.minute) < (
            config.reminder_hour,
            config.reminder_minute,
        ):
            return 0
        today_key = local_now.date().isoformat()
        if await self._storage.get_preference(f"digest.{today_key}"):
            return 0
        day_start = to_utc(local_now.replace(hour=0, minute=0, second=0, microsecond=0))
        soon = await self._approaching_actions(day_start, config.reminder_days_before)
        if not soon:
            # nothing approaching today: mark the day as digested anyway
            await self._storage.set_preference(f"digest.{today_key}", "fired")
            return 0
        today_count = sum(1 for item in soon if item.due_at < day_start + timedelta(days=1))
        await self._events.emit(
            f"{_EVENT_PREFIX}action.digest",
            date=today_key,
            today_count=today_count,
            upcoming_count=len(soon) - today_count,
            items=soon,
        )
        # push the digest straight to chat-capable notifiers: the events
        # had NO subscriber in the default deployment, so the daily
        # summary never reached QQ/WeChat users at all
        await self._push_text(self._format_digest(today_key, soon))
        reminder_logger.warning(
            self._rt(
                "reminder.digest_log",
                date=today_key,
                today=today_count,
                soon=len(soon) - today_count,
                days=config.reminder_days_before,
            )
            or f"DIGEST {today_key}: {today_count} due today, "
            f"{len(soon) - today_count} approaching in {config.reminder_days_before} days"
        )
        await self._storage.set_preference(f"digest.{today_key}", "fired")
        return 1

    async def _fire_hourly_summary(self, now: datetime, config: GeneralConfig) -> int:
        """Every hour (once per clock hour, persisted), summarize the mail
        received during that hour with the LLM and push the briefing through
        the notifiers that opted in (``options.hourly_summary``). An hour
        with no mail is marked and stays silent."""
        if not config.hourly_summary:
            return 0
        local_now = now.astimezone(ZoneInfo(config.timezone))
        # summarize the LAST CLOSED hour: this tick fires every
        # reminder_interval, so the boundary hour is caught on the first
        # tick after it ends and the open hour is never partially summed
        hour_start = to_utc(
            local_now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        )
        hour_end = hour_start + timedelta(hours=1)
        hour_key = hour_start.strftime("%Y-%m-%dT%H:00")
        if await self._storage.get_preference(f"hourly_summary.{hour_key}"):
            return 0
        # mark FIRST: an LLM failure must not re-run the hour on every tick
        await self._storage.set_preference(f"hourly_summary.{hour_key}", "fired")
        mails = [
            record
            for record in await self._storage.list_mails()
            if hour_start <= record.mail.received_at < hour_end
        ]
        opted_in = [
            notifier
            for notifier, notifier_config in zip(
                self._notifiers, self._notifier_configs, strict=True
            )
            if notifier_config.options.get("hourly_summary")
        ]
        if not mails or not opted_in:
            return 0
        briefing = await self._summarize_hour(mails, hour_start, hour_end)
        if not briefing:
            return 0
        for notifier, notifier_config in zip(self._notifiers, self._notifier_configs, strict=True):
            if notifier not in opted_in:
                continue
            push = getattr(notifier, "push_text", None)
            if push is None:
                continue
            try:
                await self._push_hourly_targeted(notifier, notifier_config, briefing)
            except Exception as exc:
                logger.warning(
                    "notifier %r hourly summary push failed: %s",
                    getattr(notifier, "backend_id", type(notifier).__name__),
                    exc,
                )
        reminder_logger.warning(
            self._rt("reminder.hourly_log", hour=hour_key, count=len(mails))
            or f"HOURLY SUMMARY {hour_key}: {len(mails)} mail(s)"
        )
        return 1

    async def _push_hourly_targeted(
        self,
        notifier: Notifier,
        notifier_config: NotifierConfig,
        briefing: str,
    ) -> None:
        """Push the hourly briefing honoring the per-chat on/off toggles:
        each configured target is skipped when its chat turned hourly off
        (``hourly_summary.chat.<provider>.<instance>.<chat_id>`` = off);
        targets never toggled default to on. Uses push-targeted sends when
        the notifier supports them, else the plain chat push."""
        raw_targets: Any = notifier_config.options.get("targets") or []
        targets = [str(t) for t in cast("list[Any]", raw_targets) if str(t).strip()]
        send_targeted = getattr(notifier, "push_to_target", None)
        if not targets or send_targeted is None:
            push = getattr(notifier, "push_text", None)
            if push is not None:
                await push(briefing)
            return
        for target in targets:
            _kind, _, chat_id = target.partition(":")
            chat_id = (chat_id or target).strip()
            pref = f"hourly_summary.chat.{notifier_config.provider}.{self._instance_for(notifier_config)}.{chat_id}"
            if await self._storage.get_preference(pref) == "off":
                continue
            try:
                await send_targeted(target, briefing)
            except Exception as exc:
                logger.warning("wechatpadpro/onebot hourly push to %s failed: %s", target, exc)

    def _instance_for(self, notifier_config: NotifierConfig) -> str:
        """The gateway instance id backing this notifier: the notifier_id
        is the instance id by guided-setup convention."""
        return notifier_config.notifier_id

    async def _summarize_hour(
        self, mails: list[MailRecord], hour_start: datetime, hour_end: datetime
    ) -> str:
        """LLM briefing for one hour of mail; '' when no router/LLM is
        wired or the request fails."""
        router = self._pipeline.router
        if router is None:
            logger.warning("hourly summary: no LLM router wired; skipping")
            return ""
        llm_ids = [llm.llm_id for llm in self._config.llms]
        if not llm_ids:
            logger.warning("hourly summary: no LLM configured; skipping")
            return ""
        from mailflow.processors import _plain_body  # pyright: ignore[reportPrivateUsage]

        lines: list[str] = []
        for record in mails[:50]:  # bound the prompt: 50 mails is plenty for an hour
            body = _plain_body(record.mail)[:400].replace("\n", " ")
            lines.append(
                f"- [{record.mail.received_at:%H:%M}] "
                f"{record.mail.sender.address}: {record.mail.subject} — "
                f"{record.summary or body}"
            )
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You write MailFlow's hourly mail briefing. You receive "
                    "the mails received in one hour. Reply with a compact "
                    "briefing in the user's language: one line per "
                    "noteworthy mail (sender, subject, why it matters); "
                    "group trivial/ad mail into a single closing line; "
                    "no greeting, no sign-off, no markdown headers. If "
                    "nothing is noteworthy, say so in one line."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Window: {hour_start:%H:00}-{hour_end:%H:00} "
                    f"({len(mails)} mail(s))\n\n" + "\n".join(lines)
                ),
            },
        ]
        try:
            completion = await router.chat(
                messages,
                primary=llm_ids[0],
                fallback=llm_ids[1:],
                options={"temperature": 0.3},
            )
            return completion.text.strip()
        except Exception as exc:
            logger.error("hourly summary LLM request failed: %s", exc)
            return ""

    async def _approaching_actions(self, day_start: datetime, days_before: int) -> list[ActionItem]:
        """Actions due within ``days_before`` days from ``day_start``, soonest first."""
        horizon = day_start + timedelta(days=days_before)
        items: list[ActionItem] = []
        for record in await self._storage.list_mails():
            items.extend(item for item in record.action_items if day_start <= item.due_at < horizon)
        items.extend(
            item
            for item in await self._storage.list_custom_actions()
            if day_start <= item.due_at < horizon
        )
        items.sort(key=lambda item: item.due_at)
        return items

    async def _fire_reminder(
        self,
        item: ActionItem,
        record: MailRecord | None,
        now: datetime,
        config: GeneralConfig,
    ) -> int:
        """Emit a once-per-window reminder for one action item when due."""
        fired = 0
        for when, kind in reminder_times(
            item,
            config.timezone,
            config.reminder_days_before,
            config.reminder_hour,
            config.reminder_minute,
        ):
            if when > now:
                continue
            key = f"reminder.{item.item_id}.{item.due_at.date().isoformat()}.{kind}"
            if await self._storage.get_preference(key):
                continue
            # Persist the fired marker BEFORE emitting: a crash (or an event
            # handler that raises) between emit and save must not cause the
            # same reminder to fire again on the next tick.
            await self._storage.set_preference(key, "fired")
            await self._events.emit(
                f"{_EVENT_PREFIX}action.reminder",
                item=item,
                record=record,
                kind=kind,
                scheduled=when,
            )
            await self._push_text(
                self._rt(
                    "reminder.push",
                    summary=item.summary,
                    due=item.time_range,
                )
                or f"【日程提醒】「{item.summary}」即将到期（{item.time_range}）"
            )
            source = record.record_id if record is not None else "user"
            if item.notes:
                message = self._rt(
                    "reminder.reminder_log_notes",
                    kind=kind,
                    range=item.time_range,
                    summary=item.summary,
                    source=source,
                    notes=item.notes,
                ) or (
                    f"REMINDER [{kind}] due {item.time_range} — "
                    f"{item.summary} (mail {source}; notes: {item.notes})"
                )
            else:
                message = (
                    self._rt(
                        "reminder.reminder_log",
                        kind=kind,
                        range=item.time_range,
                        summary=item.summary,
                        source=source,
                    )
                    or f"REMINDER [{kind}] due {item.time_range} — {item.summary} (mail {source})"
                )
            reminder_logger.warning(message)
            fired += 1
        return fired

    async def _push_text(self, text: str) -> None:
        """Deliver a plain-text push through every notifier that supports
        chat pushes (onebot etc.); mail-only notifiers skip silently."""
        for notifier in self._notifiers:
            push = getattr(notifier, "push_text", None)
            if push is None:
                continue
            try:
                await push(text)
            except Exception as exc:
                logger.warning(
                    "notifier %r text push failed: %s",
                    getattr(notifier, "backend_id", type(notifier).__name__),
                    exc,
                )

    def _format_digest(self, date_key: str, items: list[ActionItem]) -> str:
        """Items FIRST, then the summary line — a chat message must open
        with the content it refers to (the inverted order was reported
        as confusing)."""
        if self._i18n is not None:
            lines = [
                self._rt(
                    "reminder.digest_item", index=index, summary=item.summary, due=item.time_range
                )
                or f"{index}. {item.summary}（{item.time_range}）"
                for index, item in enumerate(items, start=1)
            ]
            header = self._rt(
                "reminder.digest_push",
                today=sum(1 for item in items if item.due_at.date().isoformat() == date_key),
                total=len(items),
            )
            if header and lines:
                return header + "\n" + "\n".join(lines)
        lines_fallback = [
            f"{index}. {item.summary}（{item.time_range}）"
            for index, item in enumerate(items, start=1)
        ]
        return "\n".join(lines_fallback)

    def _rt(self, key: str, **params: Any) -> str:
        """Translate a user-visible runtime message via the service i18n;
        '' when no i18n is wired or the key is missing (the caller's
        fallback template at the call site applies). ``I18n.t`` returns
        the key itself on a miss — echoing an internal key to a chat
        push is worse than the English fallback, so that case counts as
        a miss here."""
        if self._i18n is None:
            return ""
        try:
            translated: Any = self._i18n.t(key, **params) if params else self._i18n.t(key)
            text = str(translated)
            return "" if text == key else text
        except Exception:
            return ""

    async def reconfigure_notifiers(
        self, notifiers: list[Notifier], notifier_configs: list[NotifierConfig]
    ) -> None:
        """Swap only the notifier list (subscription changes). Sources,
        pipeline, workers and schedulers keep running — a full
        reconfigure here stalled chat command replies for seconds and
        dropped in-flight work."""
        self._notifiers = list(notifiers)
        self._notifier_configs = list(notifier_configs)
        await self._events.emit(f"{_EVENT_PREFIX}runtime.reconfigured")
        logger.info("runtime notifiers rebuilt: %d", len(notifiers))

    # -- status -----------------------------------------------------------------------

    async def wait_idle(self, timeout_seconds: float = 5.0) -> bool:
        """Wait until every queued mail has been processed (queue drained)."""
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout_seconds)
            return True
        except TimeoutError:
            logger.warning("queue did not drain within %.1fs", timeout_seconds)
            return False

    @property
    def started_at(self) -> datetime | None:
        return self._started_at

    def account_status(self, account_id: str) -> str:
        return self._account_status.get(account_id, "stopped")

    def account_error(self, account_id: str) -> str | None:
        return self._account_errors.get(account_id)


__all__ = ["MailFlowRuntime", "seconds_until_next_cleanup"]
