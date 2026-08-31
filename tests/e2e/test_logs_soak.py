"""Soak test: drive the real app for a while while pumping log lines and
watching that the event loop stays responsive and the render work stays
bounded. This catches event-loop starvation / unbounded growth that unit
tests with instant teardown miss (the PVE-container freeze symptom)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from mailflow_tui.app import LogsPane
from test_splash import make_app
from textual.widgets import TabbedContent


async def _tick_once(app: Any, pilot: Any) -> float:
    """One message-pump cycle; returns elapsed wall seconds."""
    start = time.perf_counter()
    await pilot.pause(0.01)
    return time.perf_counter() - start


@pytest.mark.asyncio
async def test_logs_soak_responsive(tmp_path: Path) -> None:
    """Pumping logs for ~8s of wall time must not starve the event loop:
    each pump cycle returns quickly and the app still answers queries."""
    app, service = await make_app(tmp_path)
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            for i in range(40):
                logs: Any = app.query_one(LogsPane)
                for _ in range(50):
                    logs._log_queue.put(
                        f"2026-09-01T10:{i % 60:02d}:00.000|INFO|mailflow.soak|line {i} filler"
                    )
                await pilot.pause(0.2)
                await _tick_once(app, pilot)
                # the app must remain responsive to DOM queries
                assert app.query_one(TabbedContent) is not None
                # buffer stays bounded
                assert len(logs._buffer) <= logs._MAX_LINES
            # a long drain cycle is the symptom of event-loop starvation
            elapsed = await _tick_once(app, pilot)
            assert elapsed < 1.0, f"event loop starved: tick took {elapsed:.2f}s"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_logs_soak_buffer_bounded(tmp_path: Path) -> None:
    """Even with way more log lines than the ring buffer, memory stays flat."""
    app, service = await make_app(tmp_path)
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            logs: Any = app.query_one(LogsPane)
            # push 3x the cap
            for i in range(logs._MAX_LINES * 3):
                logs._log_queue.put(f"2026-09-01T10:00:00.000|WARNING|mailflow.soak|line {i}")
                if i % 500 == 0:
                    await pilot.pause(0.05)
            await pilot.pause(0.5)
            assert len(logs._buffer) <= logs._MAX_LINES
    finally:
        await service.stop()
