"""Bounded shutdown for tasks owned by a service or plugin."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TypeVar

logger = logging.getLogger("mailflow.lifecycle")
_T = TypeVar("_T")


def _observe(task: asyncio.Future[Any]) -> None:
    if not task.cancelled():
        task.exception()


async def cancel_tasks(
    tasks: Iterable[asyncio.Task[Any]], *, timeout_seconds: float = 2.0
) -> set[asyncio.Task[Any]]:
    """Cancel owned tasks without waiting forever for cancellation handlers."""
    owned = set(tasks)
    for task in owned:
        task.add_done_callback(_observe)
        if not task.done():
            task.cancel()
    if not owned:
        return set()
    _done, pending = await asyncio.wait(owned, timeout=timeout_seconds)
    if pending:
        logger.warning("tasks did not stop: %s", ", ".join(t.get_name() for t in pending))
    return pending


async def bounded(operation: Awaitable[_T], *, timeout_seconds: float, label: str) -> _T:
    """Unlike wait_for, a timeout also bounds a reluctant cancellation handler."""
    task = asyncio.ensure_future(operation)
    task.add_done_callback(_observe)
    try:
        _done, pending = await asyncio.wait({task}, timeout=timeout_seconds)
        if not pending:
            return task.result()
    except asyncio.CancelledError:
        task.cancel()
        raise
    task.cancel()
    raise TimeoutError(f"{label} timed out after {timeout_seconds:g}s")


class OwnedListener:
    """An HTTP listener owns its accepted connections as well as its socket."""

    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[asyncio.Task[Any]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._listener_lock = asyncio.Lock()
        self._listener_stopping = False

    @property
    def running(self) -> bool:
        return self._server is not None and self._server.is_serving()

    async def start_listener(
        self,
        handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
        host: str,
        port: int,
    ) -> None:
        async with self._listener_lock:
            if self.running:
                return
            self._listener_stopping = False

            async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                task = asyncio.current_task()
                if task is not None:
                    self._connections.add(task)
                self._writers.add(writer)
                try:
                    if not self._listener_stopping:
                        await handler(reader, writer)
                finally:
                    writer.close()
                    self._writers.discard(writer)
                    if task is not None:
                        self._connections.discard(task)

            self._server = await asyncio.start_server(handle, host, port)

    async def stop_listener(self) -> None:
        async with self._listener_lock:
            self._listener_stopping = True
            server = self._server
            if server is not None:
                self._server = None
                server.close()
            for writer in list(self._writers):
                writer.close()
            await cancel_tasks(
                task for task in self._connections if task is not asyncio.current_task()
            )
            if server is not None:
                await bounded(server.wait_closed(), timeout_seconds=2.0, label="gateway listener")
