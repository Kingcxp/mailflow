"""Wait for an observable UI condition, without using machine-speed thresholds."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Any

import httpx
import pytest
from textual.css.query import NoMatches


def mock_http_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport) -> None:
    client_type = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return client_type(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


async def until(
    pilot: Any,
    condition: Callable[[], bool | Awaitable[bool]],
    *,
    deadline_seconds: float = 8,
) -> None:
    async with asyncio.timeout(deadline_seconds):
        while True:
            try:
                result = condition()
                if isawaitable(result):
                    result = await result
            except NoMatches:
                # A new screen can be current before its children have mounted.
                result = False
            if result:
                return
            await pilot.pause(0.05)
