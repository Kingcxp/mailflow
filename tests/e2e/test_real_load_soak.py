"""Realistic-load soak: exercises the same event loop as the TUI with mail
processing (sqlite + pipeline), storage reads and log flow, watching that a
single pump cycle stays fast. The PVE-container symptom is a whole-UI
freeze; code suspects are synchronous sqlite on the shared asyncio loop and
render bursts from processed-mail events."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from test_splash import make_app
from textual.widgets import TabbedContent


async def _tick(app: Any, pilot: Any) -> float:
    start = time.perf_counter()
    await pilot.pause(0.01)
    return time.perf_counter() - start


@pytest.mark.asyncio
async def test_storage_roundtrip_does_not_starve_loop(tmp_path: Path) -> None:
    """Storage reads on the loop (sync sqlite backend) must stay fast."""
    app, service = await make_app(tmp_path)
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            for _ in range(40):
                await service.list_mails()
                service.snapshot()
                elapsed = await _tick(app, pilot)
                assert elapsed < 0.5, f"loop starved: tick {elapsed:.2f}s"
                assert app.query_one(TabbedContent) is not None
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_mail_processing_burst_keeps_loop_responsive(tmp_path: Path) -> None:
    """Processing a burst of mails (pipeline + sqlite + processed events)
    must not pile renders into a stall."""
    from mailflow_testkit.fakes import make_mail

    app, service = await make_app(tmp_path)
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            for i in range(25):
                mail = make_mail(
                    message_id=f"burst-{i}",
                    account_id="acct-1",
                    subject=f"Burst mail {i}",
                    body_text=f"body {i}",
                )
                await service.process_mail(mail, force=False)
                await _tick(app, pilot)
                assert app.query_one(TabbedContent) is not None
            elapsed = await _tick(app, pilot)
            assert elapsed < 1.0, f"event-loop starved: tick {elapsed:.2f}s"
    finally:
        await service.stop()
