"""Run one real smart action against a configured endpoint and print the loop.

Takes a config path and an instruction, so a live check of the tool-calling
loop is repeatable instead of a one-off script: point it at a copy of the
development config (and a copy of its database) to avoid touching real state.

    uv run python tools/verify_smart_action_live.py tmp-verify/verify.toml "..."
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from mailflow.config import load_config
from mailflow.service import start_service
from mailflow_bundled import create_plugin_manager

DEFAULT_INSTRUCTION = (
    "帮我找到所有目前仍未过期的seminar，我打算选一点参加，"
    "要求邮件中一定要出现seminar这个关键字，不然不算seminar，不区分大小写，"
    "之后，把它们添加到日程，标题里面标注 [SEMINAR]"
)


async def main() -> None:
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("configs/development.toml")
    instruction = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_INSTRUCTION
    config = load_config(config_path)
    service = await start_service(
        config,
        config_path=config_path,
        plugin_manager=create_plugin_manager(config, discover_external=False),
        discover_plugins=False,
        enable_logging=False,
    )
    try:
        print("stored mail:", await service.count_mails())
        steps: list[str] = []

        def progress(stage: str, done: int, total: int, detail: object) -> None:
            steps.append(f"{stage} {done}/{total} {detail}")

        result = await service.smart_action(instruction, progress=progress)
        print("\n=== tool_steps ===")
        for step in result.tool_steps:
            print(" ", step)
        print("\n=== final_text ===")
        print(" ", result.final_text)
        print("\n=== records ===", len(result.records))
        for record in result.records[:10]:
            print(f"  {record.record_id} {record.mail.subject!r}")
        print("\n=== pending ===", len(result.pending))
        for pending in result.pending:
            print(f"  {pending.tool} {pending.arguments}")
        print("\n=== completeness ===")
        print(
            f"  total={result.total_mails} failed={result.failed_mails} "
            f"batches={result.failed_batches} reasons={result.failure_reasons}"
        )

        # apply exactly what was staged (mirrors the TUI's confirmed handler)
        applied = 0
        for pending in result.pending:
            if pending.tool == "schedule_seminar":
                title = str(pending.arguments.get("title") or "").strip()
                item = await service.import_seminar(
                    str(pending.arguments["candidate_id"]), title=title or None
                )
            elif pending.tool == "schedule_event":
                from datetime import datetime

                item = await service.add_action(
                    str(pending.arguments["title"]),
                    datetime.fromisoformat(str(pending.arguments["starts_at"])),
                )
            else:
                print("  (not applied here)", pending.tool)
                continue
            applied += 1
            print(f"  applied {pending.tool} -> {item.summary!r} at {item.due_at.isoformat()}")
        print("applied:", applied)

        print("\n=== schedule now ===")
        for item in await service.list_actions():
            print(f"  {item.due_at.isoformat()} {item.action_type} {item.summary!r}")
    finally:
        await service.stop()


asyncio.run(main())
