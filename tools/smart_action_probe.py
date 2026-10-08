"""Seed a temp DB with N real mails, then run the real smart action over them.

Reproduces what the user does: fetch a large batch from the mailbox, store it,
then ask the smart action to find and schedule the seminars. Prints the tool
steps the model chose, the candidates it saw, and what it staged — so a low
count can be traced to the step that lost it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mailflow.config import load_config
from mailflow.domain import MailRecord


async def main() -> None:
    config_path = Path(sys.argv[1])
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 290
    instruction = (
        sys.argv[3]
        if len(sys.argv) > 3
        else "帮我找到所有目前仍未过期的seminar，我打算选一点参加，之后把它们添加到日程"
    )

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
        print(f"fetching {limit} mail(s) from {account} (read-only)…")
        messages = await service.fetch_history(account, limit=limit)
        storage = service.storage
        for mail in messages:
            record_id = mail.normalized_message_id()
            if await storage.get_mail(record_id) is None:
                await storage.save_mail(MailRecord(record_id=record_id, mail=mail))
        stored = await service.count_mails()
        print(f"stored {stored} mail(s)")

        def progress(stage: str, done: int, total: int, detail: object) -> None:
            print(f"  · {stage} {done}/{total} {detail}", flush=True)

        result = await service.smart_action(instruction, progress=progress)

        print("\n--- tool steps the model chose ---")
        for step in result.tool_steps:
            print("  ", step[:160])
        print("\n--- final text ---")
        print(" ", result.final_text[:700])
        print("\n--- counts ---")
        print("  records surfaced:", len(result.records))
        print("  staged operations:", len(result.pending))
        for pending in result.pending:
            print("   ", pending.tool, str(pending.arguments)[:120])
        print("  total_mails:", result.total_mails, "failed:", result.failed_mails)

        candidates = await service.list_seminar_candidates()
        upcoming = [c for c in candidates if c.starts_at is not None]
        print(f"\n  stored candidates: {len(candidates)} (with a time: {len(upcoming)})")
    finally:
        await service.stop()


asyncio.run(main())
