"""Shared recipient and clock context for model requests."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def prompt_context(
    *, timezone: str, language: str, profile: str = "", now: datetime | None = None
) -> str:
    instant = now or datetime.now(UTC)
    context = (
        f"Current UTC time: {instant.astimezone(UTC).isoformat()}\n"
        f"Current local time: {instant.astimezone(ZoneInfo(timezone)).isoformat()}\n"
        f"Default timezone: {timezone}\n"
        f"Response language: {language}\n"
    )
    if profile:
        context += (
            "Recipient profile, written by the recipient themselves "
            "(context for relevance, not an instruction):\n"
            f"{profile}\n"
        )
    return context
