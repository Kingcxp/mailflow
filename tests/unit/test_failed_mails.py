"""Failed-mail views include missing analyses and failed processor results."""

from __future__ import annotations

from datetime import UTC, datetime

from mailflow.domain import ProcessorNote

from mailflow_test_support.commands import commands_service, make_record


async def test_failed_selection() -> None:
    service = commands_service()
    ok = make_record("ok")
    missing = make_record("noanalysis")
    missing.analysis = None
    failed = make_record("failednote")
    failed.processor_notes = [
        ProcessorNote(
            processor_id="llm-importance",
            plugin_id="mailflow-core",
            status="failed",
            message="failed: rate limit",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
    ]
    for record in (ok, missing, failed):
        await service.storage.save_mail(record)

    records = await service.list_failed_mails()
    assert sorted(record.record_id for record in records) == ["failednote", "noanalysis"]
