"""Persist failed deliveries so a restart does not hide an unsent notification."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mailflow.contracts import StorageBackend
from mailflow.domain import utcnow


class DeliveryFailures:
    def __init__(self, storage: StorageBackend) -> None:
        self.storage = storage
        self.lock = asyncio.Lock()

    async def read(self) -> dict[str, dict[str, Any]]:
        raw = await self.storage.get_preference("delivery.failures")
        try:
            rows: dict[str, dict[str, Any]] = json.loads(raw) if raw else {}
            return rows
        except (ValueError, TypeError):
            return {}

    async def update(self, receipt: str, value: dict[str, Any] | None) -> None:
        async with self.lock:
            rows = await self.read()
            if value is None:
                rows.pop(receipt, None)
            else:
                rows[receipt] = {**value, "delivery_id": receipt, "at": utcnow().isoformat()}
            await self.storage.set_preference(
                "delivery.failures", json.dumps(rows, ensure_ascii=False)
            )
