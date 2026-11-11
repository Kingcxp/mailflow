"""One sending rhythm for each bot instance, shared by notifications and replies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from weakref import WeakKeyDictionary

from mailflow.config import NotifierConfig
from mailflow.permissions import normalize_provider


@dataclass
class _Slot:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_at: float = 0


_slots: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, _Slot]] = WeakKeyDictionary()


async def pace(config: NotifierConfig) -> None:
    loop = asyncio.get_running_loop()
    key = normalize_provider(config.provider) + ":" + config.notifier_id
    slot = _slots.setdefault(loop, {}).setdefault(key, _Slot())
    interval = max(0.0, float(config.options.get("send_interval_seconds", 2)))
    async with slot.lock:
        while (delay := slot.next_at - loop.time()) > 0:  # noqa: ASYNC110 -- cooldown can extend during the wait
            await asyncio.sleep(delay)
        slot.next_at = loop.time() + interval


def defer(config: NotifierConfig, seconds: float) -> None:
    loop = asyncio.get_running_loop()
    key = normalize_provider(config.provider) + ":" + config.notifier_id
    slot = _slots.setdefault(loop, {}).setdefault(key, _Slot())
    slot.next_at = max(slot.next_at, loop.time() + max(0, seconds))
