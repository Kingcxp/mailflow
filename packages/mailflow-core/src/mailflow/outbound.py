"""One sending rhythm for each bot instance, shared by notifications and replies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from weakref import WeakKeyDictionary

from mailflow.config import NotifierConfig
from mailflow.permissions import normalize_provider

MAX_TEXT_UTF8_BYTES = 1_600
MAX_TEXT_UTF16_UNITS = 1_600


def text_size(text: str) -> tuple[int, int]:
    """Return UTF-8 bytes and UTF-16 code units without allocating encodings."""
    utf8_bytes = 0
    utf16_units = 0
    for char in text:
        codepoint = ord(char)
        utf8_bytes += (
            1 if codepoint <= 0x7F else 2 if codepoint <= 0x7FF else 3 if codepoint <= 0xFFFF else 4
        )
        utf16_units += 1 if codepoint <= 0xFFFF else 2
    return utf8_bytes, utf16_units


def split_text(
    text: str,
    *,
    max_utf8_bytes: int = MAX_TEXT_UTF8_BYTES,
    max_utf16_units: int = MAX_TEXT_UTF16_UNITS,
) -> list[str]:
    """Fit chat and notification text to portable pages without dropping content."""
    if not text:
        return [text]
    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = start
        last_boundary = start
        utf8_bytes = 0
        utf16_units = 0
        while end < length:
            codepoint = ord(text[end])
            byte_cost = (
                1
                if codepoint <= 0x7F
                else 2
                if codepoint <= 0x7FF
                else 3
                if codepoint <= 0xFFFF
                else 4
            )
            unit_cost = 1 if codepoint <= 0xFFFF else 2
            if utf8_bytes + byte_cost > max_utf8_bytes or utf16_units + unit_cost > max_utf16_units:
                break
            utf8_bytes += byte_cost
            utf16_units += unit_cost
            end += 1
            if text[end - 1].isspace():
                last_boundary = end
        if end == length:
            chunks.append(text[start:end])
            break
        if end == start:
            end += 1
        split_at = last_boundary if last_boundary > start else end
        chunks.append(text[start:split_at])
        start = split_at
    return chunks


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
