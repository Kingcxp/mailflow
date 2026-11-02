"""Wait for an observable UI condition, without using machine-speed thresholds."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


async def until(pilot: Any, condition: Callable[[], bool], *, deadline_seconds: float = 8) -> None:
    async with asyncio.timeout(deadline_seconds):
        while not condition():
            await pilot.pause(0.05)
