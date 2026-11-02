"""Burst handling is checked through stored results, displayed data and user controls.

These are bounded component integration tests, not performance benchmarks.
"""

from __future__ import annotations

import asyncio
import queue
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from mailflow.commands import CommandRouter
from mailflow_testkit.fakes import make_mail
from mailflow_tui.app import MailFlowApp
from mailflow_tui.log_export import LogExportScreen
from textual.widgets import (
    Button,
    DataTable,
    DirectoryTree,
    Input,
    RichLog,
    Select,
    Static,
    TabbedContent,
)

from mailflow_test_support.settings import start_service_quiet
from mailflow_test_support.ui import until


def log_text(app: MailFlowApp) -> str:
    return "\n".join(line.text for line in app.query_one("#log-view", RichLog).lines)


async def test_mail_burst_is_persisted_searchable_and_readable(tmp_path: Path) -> None:
    service = await start_service_quiet(tmp_path)
    CommandRouter(service)
    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test(size=(140, 45)) as pilot:
            mails = [
                make_mail(
                    message_id=f"burst-{index}",
                    subject=f"Burst mail {index:02d}",
                    body_text=f"Original burst body {index:02d}",
                    received_at=datetime.now(UTC),
                )
                for index in range(25)
            ]
            records = await asyncio.gather(*(service.process_mail(mail) for mail in mails))
            assert all(record is not None for record in records)
            assert {record.mail.message_id for record in await service.list_mails()} == {
                mail.message_id for mail in mails
            }
            table = cast(DataTable[str], app.query_one("#mail-table", DataTable))
            await until(pilot, lambda: table.row_count == len(mails))
            search = app.query_one("#mail-search", Input)
            search.value = "Burst mail 24"
            await until(pilot, lambda: table.row_count == 1)
            assert "Burst mail 24" in str(table.get_row_at(0))
            await until(
                pilot,
                lambda: (
                    "Original burst body 24" in str(app.query_one("#mail-body", Static).render())
                ),
            )
            search.value = ""
            await until(pilot, lambda: table.row_count == len(mails))
            tabs = app.query_one(TabbedContent)
            tabs.active = "tab-actions"
            await pilot.pause()
            tabs.active = "tab-mail"
            await pilot.pause()
            assert table.row_count == len(mails)
    finally:
        await service.stop()


async def test_log_burst_retains_recent_lines_filters_and_exports(tmp_path: Path) -> None:
    service = await start_service_quiet(tmp_path)
    log_queue: queue.Queue[str] = queue.Queue()
    app = MailFlowApp(service, log_queue)
    try:
        async with app.run_test(size=(140, 45)) as pilot:
            app.query_one(TabbedContent).active = "tab-logs"
            await pilot.pause()
            app.query_one("#log-level", Select).value = "WARNING"
            await pilot.pause()
            for index in range(3000):
                log_queue.put(f"10:00|ERROR|mailflow.runtime|burst-{index:04d}")
            log_queue.put("10:00|INFO|mailflow.runtime|info-only-marker")
            await until(pilot, lambda: "burst-2999" in log_text(app))
            assert "burst-0000" not in log_text(app)
            assert "info-only-marker" not in log_text(app)
            app.query_one("#log-search", Input).value = "burst-2999"
            await until(
                pilot, lambda: "burst-2999" in log_text(app) and "burst-2998" not in log_text(app)
            )
            app.query_one("#log-level", Select).value = "INFO"
            app.query_one("#log-search", Input).value = "info-only-marker"
            await until(pilot, lambda: "info-only-marker" in log_text(app))
            assert "burst-2999" not in log_text(app)

            app.query_one("#logs-export", Button).press()
            await until(pilot, lambda: isinstance(app.screen, LogExportScreen))
            tree = app.screen.query_one("#log-export-tree", DirectoryTree)
            tree.path = tmp_path
            await pilot.pause(0.3)
            tree.cursor_line = 0
            app.screen.query_one("#log-export-filename", Input).value = "burst-export"
            app.screen.query_one("#log-export-save", Button).press()
            target = tmp_path / "burst-export.log"
            await until(pilot, target.is_file)
            exported = target.read_text(encoding="utf-8")
            assert "burst-2999" in exported and "info-only-marker" in exported
            assert "burst-0000" not in exported
            assert len(exported.splitlines()) < 3001
    finally:
        await service.stop()
