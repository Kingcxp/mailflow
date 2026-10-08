"""Audit where an event keyword actually lives, and how much survives "upcoming".

Answers three questions with one dataset, instead of quoting numbers from
different runs:

1. which field carries the keyword — subject, plain body, HTML body, attachment
   filename, or nowhere in the text at all (a poster image may be the only place
   the word appears, which no text search can see);
2. how many mails look like an event notice by date pattern while carrying no
   keyword in any text field — the population that image-only posters would
   explain;
3. of the candidates the real scan produces, how many are still upcoming, split
   by whether their mail contained the literal keyword.

Usage:
    uv run python tools/mail_keyword_audit.py tmp-verify/audit.toml [--limit 290]
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from mailflow.config import load_config
from mailflow.domain import Attachment, MailMessage, MailRecord, SeminarStatus, to_utc, utcnow

TERMS = ("seminar", "lecture", "colloquium", "workshop", "webinar", "symposium", "talk")

# "15 September", "2026-09-15", "15/09/2026", "10月15日", "Sep 15"
_DATE_PATTERNS = (
    r"\b\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    r"\b\d{4}-\d{2}-\d{2}\b",
    r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
    r"\b\d{1,2}\s*月\s*\d{1,2}\s*日\b",
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b",
)


def _fields(mail: MailMessage) -> dict[str, str]:
    attachments: list[Attachment] = list(mail.attachments or [])
    return {
        "subject": (mail.subject or "").casefold(),
        "body_text": (mail.body_text or "").casefold(),
        "body_html": (mail.body_html or "").casefold(),
        "attachment_name": " ".join(str(item.filename or "") for item in attachments).casefold(),
    }


def _has_date_pattern(text: str) -> bool:
    lowered = text.casefold()
    return any(re.search(pattern, lowered) for pattern in _DATE_PATTERNS)


async def main() -> None:
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("configs/development.toml")
    limit = 290
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
        account = service.history_accounts()[0]
        print(f"fetching {limit} mail(s) from {account}…")
        messages = await service.fetch_history(account, limit=limit)
        print(f"fetched {len(messages)} message(s)\n")

        where: dict[str, int] = dict.fromkeys(("subject", "body_text", "body_html"), 0)
        attachment_name_hits = 0
        text_hits = 0
        image_mails = 0
        no_keyword_but_dated: list[MailMessage] = []
        keyword_in_text: list[MailMessage] = []
        keyword_only_in_image_guess: list[MailMessage] = []

        for mail in messages:
            fields = _fields(mail)
            hit_terms = [term for term in TERMS if any(term in value for value in fields.values())]
            bare = [
                term
                for term in TERMS
                if any(term in fields[key] for key in ("subject", "body_text", "body_html"))
            ]
            for key in where:
                if any(term in fields[key] for term in TERMS):
                    where[key] += 1
            if any(term in fields["attachment_name"] for term in TERMS):
                attachment_name_hits += 1
            if bare:
                text_hits += 1
                keyword_in_text.append(mail)
            images = [
                item
                for item in (mail.attachments or [])
                if str(item.content_type).startswith("image/")
            ]
            if images:
                image_mails += 1
            # an event-looking mail whose only possible keyword carrier is an
            # image: it states a date, has a poster, and no term in its text
            if not bare and images and _has_date_pattern(f"{mail.subject} {mail.body_text}"):
                keyword_only_in_image_guess.append(mail)
            elif (
                not hit_terms
                and not images
                and _has_date_pattern(f"{mail.subject} {mail.body_text}")
            ):
                no_keyword_but_dated.append(mail)

        print("=== where the keyword lives (any of the terms) ===")
        for key, count in where.items():
            print(f"  {key:16s}: {count}")
        print(f"  attachment_name : {attachment_name_hits}")
        print(f"  any text field  : {text_hits} of {len(messages)}")
        print(f"\nmails carrying image attachments: {image_mails}")
        print(
            "event-looking mails with an image and no keyword in any text field "
            f"(poster-only candidates): {len(keyword_only_in_image_guess)}"
        )
        for mail in keyword_only_in_image_guess[:25]:
            names = [
                str(item.filename or "unnamed")
                for item in (mail.attachments or [])
                if str(item.content_type).startswith("image/")
            ]
            print(f"    {mail.subject[:66]!r}")
            print(f"        images: {names[:2]}")

        print(
            "\nevent-looking mails with neither keyword nor image (plain text): "
            f"{len(no_keyword_but_dated)}"
        )

        records = [
            MailRecord(record_id=mail.normalized_message_id(), mail=mail) for mail in messages
        ]
        discovery = await service.discover_seminars(records=records)
        now = utcnow()
        pending = [c for c in discovery.candidates if c.status is SeminarStatus.PENDING]
        upcoming = [c for c in pending if c.starts_at is not None and to_utc(c.starts_at) > now]
        untimed = [c for c in pending if c.starts_at is None]
        expired = [c for c in pending if c.starts_at is not None and to_utc(c.starts_at) <= now]
        keyword_ids = {mail.normalized_message_id() for mail in keyword_in_text}
        upcoming_with_keyword = [c for c in upcoming if c.mail_id in keyword_ids]

        print("\n=== the scan over these mails ===")
        print(f"  evaluated           : {discovery.evaluated_mails} of {discovery.total_mails}")
        print(f"  failed batches      : {discovery.failed_batches}")
        print(f"  candidates (pending): {len(pending)}")
        print(f"    still upcoming    : {len(upcoming)}")
        print(f"    already past      : {len(expired)}")
        print(f"    no time in the mail: {len(untimed)}")
        print("\n=== cross-tab: upcoming candidates ===")
        print(f"  from a mail containing the term : {len(upcoming_with_keyword)}")
        print(f"  from a mail without it          : {len(upcoming) - len(upcoming_with_keyword)}")
        print(
            "\n(the instruction that demanded the literal word could only ever "
            f"reach the first of those two: {len(upcoming_with_keyword)})"
        )
    finally:
        await service.stop()


asyncio.run(main())
