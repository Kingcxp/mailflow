"""Fetch a large batch of real mail and exercise the seminar path end to end.

Answers "is the seminar scan finding everything it should?" on a real mailbox
rather than on the handful of mails a normal fetch stores. It:

1. pulls ``limit`` messages through the account's ``fetch_history``
   (read-only: nothing is emitted, nothing is stored),
2. runs the real seminar discovery over them in the same batches the service
   uses,
3. reports every candidate, why each is or is not schedulable, and how many
   mails each batch could read,
4. prints how many of the mails look like an event notice by literal keyword,
   so the scan's recall can be compared against a cheap independent check.

Usage:
    uv run python tools/seminar_scan_probe.py tmp-verify/scan.toml [--limit 200]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from mailflow.config import load_config
from mailflow.domain import MailRecord, SeminarStatus, to_utc, utcnow

KEYWORDS = (
    "seminar",
    "lecture",
    "colloquium",
    "workshop",
    "webinar",
    "symposium",
    "talk",
    "briefing",
    "forum",
    "series",
)


def looks_like_event(mail: Any) -> list[str]:
    text = " ".join(
        (
            str(getattr(mail, "subject", "")),
            str(getattr(mail, "body_text", "")),
            str(getattr(mail, "body_html", "")),
        )
    ).casefold()
    return [word for word in KEYWORDS if word in text]


async def main() -> None:
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("configs/development.toml")
    limit = 200
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    from mailflow.service import start_service
    from mailflow_bundled import create_plugin_manager

    config = load_config(config_path)
    service = await start_service(
        config,
        config_path=config_path,
        plugin_manager=create_plugin_manager(config, discover_external=False),
        discover_plugins=False,
        enable_logging=False,
    )
    try:
        accounts = service.history_accounts()
        if not accounts:
            print("no account advertises history capability")
            return
        account_id = accounts[0]
        print(f"fetching up to {limit} mail(s) from {account_id} (read-only)…")
        messages = await service.fetch_history(account_id, limit=limit)
        print(f"fetched {len(messages)} message(s)")

        records = [
            MailRecord(record_id=mail.normalized_message_id(), mail=mail) for mail in messages
        ]
        keyword_hits: dict[str, list[str]] = {
            record.record_id: looks_like_event(record.mail) for record in records
        }
        literal = {rid for rid, words in keyword_hits.items() if words}
        print(
            f"mails whose text carries an event keyword: {len(literal)} (of {len(records)} fetched)"
        )

        batches_seen: list[str] = []

        def report(stage: str, done: int, total: int, detail: Any) -> None:
            batches_seen.append(f"{stage} {done}/{total} {detail}")

        discovery = await service.discover_seminars(records=records, progress=report)
        print(
            f"\ndiscovery: {discovery.total_mails} mail(s), evaluated "
            f"{discovery.evaluated_mails}, failed {discovery.failed_mails} in "
            f"{discovery.failed_batches} batch(es)"
        )
        for reason in discovery.failure_reasons:
            print(f"  failure: {reason}")

        now = utcnow()
        schedulable: list[Any] = []
        for candidate in discovery.candidates:
            if candidate.status is not SeminarStatus.PENDING:
                continue
            if candidate.starts_at is None:
                state = "no time in the mail — review only"
            elif to_utc(candidate.starts_at) <= now:
                state = f"already past ({candidate.starts_at.isoformat()})"
            else:
                state = f"upcoming ({candidate.starts_at.isoformat()})"
                schedulable.append(candidate)
            title = candidate.title[:58]
            print(
                f"  [{state}] {title!r} conf={candidate.confidence} mail={candidate.mail_id[:28]}"
            )

        print(f"\nupcoming (could be added to the schedule): {len(schedulable)}")

        # which keyword mails produced no candidate at all: the recall gap
        found_mail_ids = {candidate.mail_id for candidate in discovery.candidates}
        missed = sorted(literal - found_mail_ids)
        print(f"\nevent-keyword mails with no candidate: {len(missed)}")
        for record_id in missed[:40]:
            record = next((item for item in records if item.record_id == record_id), None)
            if record is not None:
                words = ",".join(keyword_hits[record_id])
                print(f"  ({words}) {record.mail.subject[:72]!r}")
    finally:
        await service.stop()


asyncio.run(main())
