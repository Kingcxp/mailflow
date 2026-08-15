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
from typing import Any
from zoneinfo import ZoneInfo

from mailflow.config import MailAccountConfig, MailFlowConfig, NotifierConfig
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
    ) -> None:
        self._config = config
        self._sources = dict(sources)
        self._pipeline = pipeline
        self._storage = storage
        self._notifiers = list(notifiers)
        self._notifier_configs = list(notifier_configs)
        self._events = events
        self._account_configs = list(account_configs)

        self._queue: asyncio.Queue[MailMessage] = asyncio.Queue(maxsize=config.general.queue_size)
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self._account_status: dict[str, str] = {}
        self._account_errors: dict[str, str | None] = {}
        self._started_at: datetime | None = None
        self._closed = False

    # -- lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        self._started_at = datetime.now()
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

    async def _process_one(self, mail: MailMessage) -> None:
        analysis, notes, _, _ = await self._pipeline.process(
            mail, mail.account_id, timezone=self._config.general.timezone
        )
        record = MailRecord(
            record_id=mail.normalized_message_id(),
            mail=mail,
            auto_urgency=analysis.urgency,
            analysis=analysis,
            processor_notes=notes,
            received_at=mail.received_at,
        )
        await self._persist_with_retry(record)
        await self._events.emit(f"{_EVENT_PREFIX}mail.processed", record=record)
        await self._notify(record)

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
            try:
                await notifier.notify(record)
            except Exception as exc:
                logger.warning("notifier failed for %r: %s", record.record_id, exc)

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
                    await self._events.emit(
                        f"{_EVENT_PREFIX}action.reminder",
                        item=item,
                        record=record,
                        kind=kind,
                        scheduled=when,
                    )
                    reminder_logger.warning(
                        "REMINDER [%s] due %s — %s (mail %s%s)",
                        kind,
                        item.time_range,
                        item.summary,
                        record.record_id,
                        f"; notes: {item.notes}" if item.notes else "",
                    )
                    await self._storage.set_preference(key, "fired")
                    fired += 1
        return fired

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
