"""Persist final assistant exchanges, never running jobs or confirmation tokens."""

from __future__ import annotations

from copy import copy
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mailflow.domain import SmartActionResult
from mailflow.workflows import actor_key

if TYPE_CHECKING:
    from mailflow.service import MailFlowService


class AssistantHistory:
    def __init__(self, service: MailFlowService) -> None:
        self.service = service
        self.pruned_on: date | None = None
        catalog = copy(service.i18n)
        self.legacy_pending: set[str] = set()
        for language in catalog.available_codes():
            catalog.set_language(language)
            self.legacy_pending.add(catalog.t("experience.history_pending"))

    async def prune(self) -> None:
        now = datetime.now(UTC)
        if self.pruned_on == now.date():
            return
        workflows = self.service.workflows
        async with workflows.lock:
            keys = await workflows.read_value("assistant.history.index", [])
            for key in keys:
                days = await workflows.read_value(key + ".days", 30)
                cutoff = (now - timedelta(days=max(0, int(days)))).isoformat()
                rows = await workflows.read_value(key, [])
                kept = [r for r in rows if days and r["at"] >= cutoff]
                if len(kept) != len(rows):
                    await workflows.write_value(key, kept)
        self.pruned_on = now.date()

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
        active = {
            history_id
            for plan_id, history_id in self.service._smart_history.items()  # pyright: ignore[reportPrivateUsage]
            if (plan := self.service._smart_plans.get(plan_id)) is not None  # pyright: ignore[reportPrivateUsage]
            and plan[1] > datetime.now(UTC)
        }
        records: list[dict[str, Any]] = []
        for row in reversed(kept):
            outcome = row.get("outcome", "")
            needs_confirmation = row.get("needs_confirmation")
            # Older records stored the confirmation state as translated text.
            if needs_confirmation is None and outcome in self.legacy_pending:
                needs_confirmation, outcome = True, ""
            if needs_confirmation:
                outcome = "\n".join(
                    filter(
                        None,
                        [
                            outcome,
                            self.service.t(
                                "experience.history_pending"
                                if row["id"] in active
                                else "experience.history_expired"
                            ),
                        ],
                    )
                )
            if (
                query
                and query not in (row["request"] + "\n" + row["answer"] + "\n" + outcome).casefold()
            ):
                continue
            records.append(
                {
                    **row,
                    "needs_confirmation": bool(needs_confirmation),
                    "outcome": outcome,
                    "answer": row["answer"] + ("\n\n" + outcome if outcome else ""),
                }
            )
        return {
            "retention_days": days,
            "records": records,
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

    async def add(self, request: str, result: SmartActionResult) -> str:
        state = await self.read({})
        if not state["retention_days"]:
            return ""
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
            "outcome": "",
            "needs_confirmation": bool(result.pending),
            "applied": 0,
        }
        workflows = self.service.workflows
        async with workflows.lock:
            key = "assistant.history." + actor_key()
            if not await workflows.read_value(key + ".days", 30):
                return ""
            rows = await workflows.read_value(key, [])
            rows.append(row)
            await workflows.write_value(key, rows)
            keys = await workflows.read_value("assistant.history.index", [])
            if key not in keys:
                await workflows.write_value("assistant.history.index", [*keys, key])
        return str(row["id"])

    async def finish(self, record_id: str, result: dict[str, Any]) -> None:
        if not record_id:
            return
        workflows = self.service.workflows
        async with workflows.lock:
            key = "assistant.history." + actor_key()
            rows = await workflows.read_value(key, [])
            row = next((r for r in rows if r["id"] == record_id), None)
            if row is None:
                return
            row["applied"] = row.get("applied", 0) + int(result.get("applied", 0))
            row["needs_confirmation"] = bool(result.get("failed"))
            row["outcome"] = (
                self.service.t("experience.history_applied", count=row["applied"])
                if row["applied"] or not result.get("cancelled")
                else ""
            )
            if result.get("cancelled"):
                row["outcome"] = "\n".join(
                    filter(None, [row["outcome"], self.service.t("tui.smart_changes_cancelled")])
                )
            if result.get("errors"):
                row["outcome"] += "\n" + "\n".join(result["errors"])
            for ident, state in result.get("source_results", {}).items():
                if state.get("state") != "synced":
                    row["outcome"] += "\n" + ident + ": " + state["message"]
            await workflows.write_value(key, rows)
