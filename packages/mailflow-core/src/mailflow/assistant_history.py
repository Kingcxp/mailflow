"""Persist final assistant exchanges, never running jobs or confirmation tokens."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mailflow.domain import SmartActionResult
from mailflow.workflows import actor_key

if TYPE_CHECKING:
    from mailflow.service import MailFlowService


class AssistantHistory:
    def __init__(self, service: MailFlowService) -> None:
        self.service = service

    async def read(self, values: dict[str, Any]) -> dict[str, Any]:
        workflows = self.service.workflows
        async with workflows.lock:
            key = "assistant.history." + actor_key()
            days = await workflows.read_value(key + ".days", 30)
            rows = await workflows.read_value(key, [])
            cutoff = (datetime.now(UTC) - timedelta(days=max(0, int(days)))).isoformat()
            kept = [r for r in rows if days and r["at"] >= cutoff]
            if len(kept) != len(rows):
                await workflows.write_value(key, kept)
        query = str(values.get("query", "")).casefold()
        return {
            "retention_days": days,
            "records": [
                r
                for r in reversed(kept)
                if not query or query in (r["request"] + "\n" + r["answer"]).casefold()
            ],
        }

    async def change(self, values: dict[str, Any]) -> dict[str, Any]:
        workflows = self.service.workflows
        async with workflows.lock:
            key = "assistant.history." + actor_key()
            if "retention_days" in values:
                await workflows.write_value(key + ".days", max(0, int(values["retention_days"])))
            rows = await workflows.read_value(key, [])
            if values.get("clear") or values.get("retention_days") == 0:
                rows = []
            elif values.get("record_id"):
                rows = [r for r in rows if r["id"] != values["record_id"]]
            await workflows.write_value(key, rows)
        return await self.read({})

    async def add(self, request: str, result: SmartActionResult) -> None:
        state = await self.read({})
        if not state["retention_days"]:
            return
        references: list[dict[str, Any]] = list(result.evidence)
        seen = {(r["kind"], r["id"]) for r in references}
        for kind, rows in [
            ("mail", [(r.record_id, r.mail.subject) for r in result.records]),
            ("action", [(a.item_id, a.summary) for a in result.actions]),
        ]:
            references.extend(
                {"kind": kind, "id": ident, "title": title, "quote": "", "verified": False}
                for ident, title in rows
                if (kind, ident) not in seen
            )
        row = {
            "id": uuid4().hex,
            "at": datetime.now(UTC).isoformat(),
            "request": request,
            "answer": result.final_text,
            "references": references,
        }
        workflows = self.service.workflows
        async with workflows.lock:
            key = "assistant.history." + actor_key()
            if not await workflows.read_value(key + ".days", 30):
                return
            rows = await workflows.read_value(key, [])
            rows.append(row)
            await workflows.write_value(key, rows)
