"""Chat subscriptions: which chats receive notifications from a gateway.

A gateway instance (napcat-1, ...) has a list of subscribed
chats (group ids or contact ids). Mail notifications are delivered to
every subscribed chat of every running instance, in addition to the
notifier's configured admin targets.

Stored in the preferences store (persisted across restarts).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from mailflow.permissions import normalize_provider

logger = logging.getLogger("mailflow.subscriptions")

_PREF_PREFIX = "gateway.sub."


class PreferenceStore(Protocol):
    async def get_preference(self, key: str) -> str | None: ...
    async def set_preference(self, key: str, value: str) -> None: ...


class Subscriptions:
    """Per-gateway-instance chat subscription registry."""

    def __init__(self, storage: PreferenceStore) -> None:
        self._storage = storage

    @staticmethod
    def _key(provider: str, instance_id: str) -> str:
        return f"{_PREF_PREFIX}{normalize_provider(provider)}.{instance_id}"

    async def subscribers(self, provider: str, instance_id: str) -> list[str]:
        """Chat ids currently subscribed to this instance."""
        data = await self.all(provider, instance_id)
        return list(dict.fromkeys(str(c) for c in data.get("subscribers", [])))

    async def add(self, provider: str, instance_id: str, chat_id: str) -> bool:
        """Subscribe a chat; True when newly added."""
        current = await self.subscribers(provider, instance_id)
        if chat_id in current:
            return False
        current.append(chat_id)
        await self._storage.set_preference(
            self._key(provider, instance_id),
            json.dumps({"subscribers": current}),
        )
        return True

    async def remove(self, provider: str, instance_id: str, chat_id: str) -> bool:
        """Unsubscribe a chat; True when it was subscribed."""
        current = await self.subscribers(provider, instance_id)
        if chat_id not in current:
            return False
        current.remove(chat_id)
        await self._storage.set_preference(
            self._key(provider, instance_id),
            json.dumps({"subscribers": current}),
        )
        return True

    async def all(self, provider: str, instance_id: str) -> dict[str, Any]:
        raw = await self._storage.get_preference(self._key(provider, instance_id))
        if raw is None and normalize_provider(provider) == "onebot":
            # Read old bridge names until the first canonical write. An explicit
            # unsubscribe writes the canonical list, so aliases cannot revive it.
            subscribers: list[str] = []
            for alias in ("napcat", "OneBot V11", "OneBot V12", "aiocqhttp"):
                legacy = await self._storage.get_preference(f"{_PREF_PREFIX}{alias}.{instance_id}")
                if not legacy:
                    continue
                try:
                    subscribers.extend(str(c) for c in json.loads(legacy).get("subscribers", []))
                except (ValueError, AttributeError):
                    logger.warning("invalid legacy subscription list for %s", alias)
            return {"subscribers": list(dict.fromkeys(subscribers))}
        if not raw:
            return {"subscribers": []}
        try:
            parsed: Any = json.loads(raw)
            if isinstance(parsed, dict):
                return {str(k): v for k, v in parsed.items()}  # pyright: ignore[reportUnknownVariableType,reportUnknownArgumentType]
            return {"subscribers": []}
        except ValueError:
            return {"subscribers": []}
