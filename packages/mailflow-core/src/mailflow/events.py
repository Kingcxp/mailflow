"""Lightweight async event bus for runtime and host clients.

Handlers are awaited concurrently; a failing handler is logged and never
silently swallowed, and never breaks the remaining handlers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("mailflow.events")

EventHandler = Callable[..., Awaitable[None]]
Unsubscribe = Callable[[], None]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = {}

    def subscribe(self, event: str, handler: EventHandler) -> Unsubscribe:
        """Subscribe to one event; returns an unsubscribe callable."""
        handlers = self._subscribers.setdefault(event, [])
        if handler not in handlers:
            handlers.append(handler)

        def unsubscribe() -> None:
            handlers = self._subscribers.get(event)
            if handlers is not None and handler in handlers:
                handlers.remove(handler)

        return unsubscribe

    def subscribe_all(self, handler: EventHandler) -> Unsubscribe:
        """Subscribe to every emitted event, including unknown names."""
        return self.subscribe("*", handler)

    async def emit(self, event: str, **payload: Any) -> None:
        handlers = list(self._subscribers.get(event, []))
        wildcard = list(self._subscribers.get("*", []))
        if not handlers and not wildcard:
            return
        results = await asyncio.gather(
            *(h(event=event, **payload) for h in handlers),
            *(h(event=event, **payload) for h in wildcard),
            return_exceptions=True,
        )
        for handler, result in zip([*handlers, *wildcard], results, strict=True):
            if isinstance(result, BaseException):
                logger.error(
                    "event handler %r failed for %r: %s",
                    getattr(handler, "__name__", handler),
                    event,
                    result,
                )


__all__ = ["EventBus", "EventHandler", "Unsubscribe"]
