"""list_failed_mails targets exactly the records whose analysis did not
complete: no analysis, or any failed processor note."""

from __future__ import annotations

from datetime import UTC
from typing import Any, cast

from mailflow.domain import MailAnalysis, MailMessage, MailRecord, Urgency
from mailflow.service import MailFlowService


class _Store:
    def __init__(self) -> None:
        self.records: dict[str, MailRecord] = {}

    async def initialize(self) -> None: ...
    async def close(self) -> None: ...
    async def list_mails(self) -> list[MailRecord]:
        return list(self.records.values())

    async def get_mail(self, record_id: str) -> MailRecord | None:
        return self.records.get(record_id)


def _record(record_id: str, *, analysis: MailAnalysis | None, failed_note: bool) -> MailRecord:
    from datetime import datetime as dt

    from mailflow.domain import MailAddress, ProcessorNote

    mail = MailMessage(
        message_id=record_id,
        account_id="acct-1",
        sender=MailAddress(address="a@example.com"),
        recipients=[],
        subject=record_id,
        body_text="b",
        date=dt.now(UTC),
        received_at=dt.now(UTC),
    )
    notes: list[ProcessorNote] = []
    if failed_note:
        notes.append(
            ProcessorNote(
                processor_id="llm-importance",
                plugin_id="mailflow-core",
                status="failed",
                message="failed: rate limit",
                started_at=dt.now(UTC),
                finished_at=dt.now(UTC),
            )
        )
    return MailRecord(
        record_id=record_id,
        mail=mail,
        auto_urgency=Urgency.INFO,
        analysis=analysis,
        processor_notes=notes,
    )


def _service(store: _Store) -> MailFlowService:
    from mailflow.config import MailFlowConfig
    from mailflow.events import EventBus
    from mailflow.i18n import I18n
    from mailflow.plugins import PluginManager
    from mailflow.registry import ComponentRegistry

    return MailFlowService(
        config=cast(Any, MailFlowConfig()),
        registry=ComponentRegistry(),
        plugin_manager=PluginManager(),
        storage=cast(Any, store),
        sources={},
        router=cast(Any, None),
        pipeline=cast(Any, None),
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        i18n=I18n(),
    )


async def test_failed_selection() -> None:
    from mailflow.domain import MailAnalysis as MA

    store = _Store()
    store.records["ok"] = _record(
        "ok", analysis=MA(summary="s", urgency=Urgency.INFO), failed_note=False
    )
    store.records["noanalysis"] = _record("noanalysis", analysis=None, failed_note=False)
    store.records["failednote"] = _record(
        "failednote", analysis=MA(summary="s", urgency=Urgency.INFO), failed_note=True
    )
    failed = await _service(store).list_failed_mails()
    assert sorted(r.record_id for r in failed) == ["failednote", "noanalysis"]
