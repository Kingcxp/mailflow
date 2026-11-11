"""Review proposed mail and task changes before applying them."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from mailflow.domain import PendingOperation
from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Static

from mailflow_tui.widgets import Input, TextArea


class SmartActionReviewModal(ModalScreen[list[PendingOperation] | None]):
    BINDINGS: ClassVar[list[Any]] = []

    def __init__(self, service: MailFlowService, operations: list[PendingOperation]) -> None:
        super().__init__()
        self._service = service
        self._operations = [operation.model_copy(deep=True) for operation in operations]
        self._labels: dict[int, Static] = {}
        self._bindings.bind("escape", "cancel", self._t("tui.smart_review_cancel"))

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def _local_time(self, value: Any) -> str:
        if not value:
            return ""
        try:
            moment = datetime.fromisoformat(str(value))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            return moment.astimezone(ZoneInfo(self._service.config.general.timezone)).strftime(
                "%Y-%m-%d %H:%M"
            )
        except (TypeError, ValueError):
            return str(value)

    def _description(self, index: int, operation: PendingOperation) -> str:
        args = operation.arguments
        if operation.summary_key == "assistant.preview":
            return self._t(operation.summary_key, **operation.summary_params)
        if operation.tool == "delete_mail":
            count = len(args.get("record_ids", []))
            subjects = " · ".join(args.get("subjects", []))
            if subjects and count > 5:
                subjects += "…"
            return self._t("tui.agent_pending_delete", count=count) + (
                f" · {subjects}" if subjects else ""
            )
        parts = [
            self._t(f"tui.smart_review_operation_{operation.tool}"),
            str(args.get("summary") or operation.summary_params.get("summary") or ""),
            self._local_time(args.get("due_at")),
            self._local_time(args.get("due_end")),
            str(args.get("location") or ""),
            str(args.get("url") or ""),
            str(args.get("notes") or ""),
            str(args.get("category") or ""),
            ", ".join(args.get("tags") or []),
            self._t("recurrence." + args["repeat"]["frequency"]) if args.get("repeat") else "",
        ]
        if args.get("completed"):
            parts.append(self._t("recurrence.complete_delete"))
        repeat: dict[str, Any] = args.get("repeat") or {}
        if repeat.get("weekdays"):
            parts.append(
                self._t("recurrence.weekdays")
                + ": "
                + ", ".join(str(day + 1) for day in repeat["weekdays"])
            )
        if repeat.get("month_day"):
            parts.append(self._t("recurrence.month_day") + f": {repeat['month_day']}")
        if operation.tool == "delete_action":
            key = (
                "tui.smart_review_delete_analysis"
                if args.get("origin") == "analysis"
                else "tui.smart_review_delete_item"
            )
            parts.append(self._t(key))
        return " · ".join(part for part in parts if part)

    def _can_edit(self, operation: PendingOperation) -> bool:
        return operation.tool in {"add_action", "edit_action"}

    def compose(self) -> ComposeResult:
        with Vertical(id="smart-review-dialog"):
            yield Static(self._t("tui.smart_review_title"), id="smart-review-title")
            with ScrollableContainer(id="smart-review-operations"):
                for index, operation in enumerate(self._operations):
                    yield Checkbox(
                        self._t("ux.include_change"), value=True, id=f"smart-review-include-{index}"
                    )
                    if operation.tool == "delete_mail":
                        subjects = operation.arguments.get("subjects", [])
                        for position, record_id in enumerate(operation.record_ids):
                            yield Checkbox(
                                str(subjects[position]) if position < len(subjects) else record_id,
                                value=True,
                                id=f"smart-review-mail-{index}-{position}",
                            )
                    with Horizontal(classes="smart-review-row"):
                        label = Static(
                            self._description(index, operation),
                            id=f"smart-review-op-{index}",
                            markup=False,
                        )
                        self._labels[index] = label
                        yield label
                        if self._can_edit(operation):
                            yield Button(
                                self._t("tui.smart_review_edit"), id=f"smart-review-edit-{index}"
                            )
            yield Static("", id="smart-review-error")
            with Horizontal(id="smart-review-buttons"):
                yield Button(self._t("tui.btn_apply"), id="smart-review-apply", variant="primary")
                yield Button(self._t("tui.smart_review_cancel"), id="smart-review-cancel")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        ident = event.button.id or ""
        if ident == "smart-review-cancel":
            self.dismiss(None)
        elif ident == "smart-review-apply":
            selected: list[PendingOperation] = []
            for index, operation in enumerate(self._operations):
                if not self.query_one(f"#smart-review-include-{index}", Checkbox).value:
                    continue
                if operation.tool == "delete_mail":
                    ids = [
                        rid
                        for pos, rid in enumerate(operation.record_ids)
                        if self.query_one(f"#smart-review-mail-{index}-{pos}", Checkbox).value
                    ]
                    if not ids:
                        continue
                    operation.record_ids = ids
                    operation.arguments["record_ids"] = ids
                selected.append(operation)
            self.dismiss(selected)
        elif ident.startswith("smart-review-edit-"):
            index = int(ident.rsplit("-", 1)[1])
            self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
                SmartOperationEditModal(self._service, self._operations[index]),
                lambda values: self._updated(index, values),
            )

    def _updated(self, index: int, values: dict[str, Any] | None) -> None:
        if values is None:
            return
        operation = self._operations[index]
        operation.arguments.update(values)
        self._labels[index].update(self._description(index, operation))

    def action_cancel(self) -> None:
        self.dismiss(None)


class SmartOperationEditModal(ModalScreen[dict[str, Any] | None]):
    BINDINGS: ClassVar[list[Any]] = []

    def __init__(self, service: MailFlowService, operation: PendingOperation) -> None:
        super().__init__()
        self._service = service
        self._operation = operation.model_copy(deep=True)
        self._bindings.bind("escape", "cancel", self._t("tui.smart_review_cancel"))

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def _time_text(self, raw: Any) -> str:
        if not raw:
            return ""
        try:
            value = datetime.fromisoformat(str(raw))
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.astimezone(ZoneInfo(self._service.config.general.timezone)).strftime(
                "%Y-%m-%d %H:%M"
            )
        except (TypeError, ValueError):
            return ""

    def compose(self) -> ComposeResult:
        args = self._operation.arguments
        with Vertical(id="smart-review-edit-dialog"):
            yield Static(self._t("tui.smart_review_edit_title"), id="smart-review-edit-title")
            with ScrollableContainer(id="smart-review-edit-fields"):
                for key, field, default in (
                    ("summary", "summary", ""),
                    ("action_type", "type", "errand"),
                    ("start", "start", ""),
                    ("end", "end", ""),
                ):
                    yield Static(self._t(f"tui.smart_review_{key}"), classes="todo-label")
                    value = (
                        self._time_text(args.get("due_at" if field == "start" else "due_end"))
                        if field in {"start", "end"}
                        else str(args.get("action_type" if field == "type" else key) or default)
                    )
                    yield Input(value=value, id=f"smart-review-{field}")
                yield Static(self._t("tui.smart_review_notes"), classes="todo-label")
                yield TextArea(str(args.get("notes") or ""), id="smart-review-notes")
                for field in ("location", "url"):
                    yield Static(self._t(f"tui.smart_review_{field}"), classes="todo-label")
                    yield Input(value=str(args.get(field) or ""), id=f"smart-review-{field}")
            yield Static("", id="smart-review-edit-error")
            with Horizontal(id="smart-review-edit-buttons"):
                yield Button(
                    self._t("tui.smart_review_save_edit"),
                    id="smart-review-save-edit",
                    variant="primary",
                )
                yield Button(self._t("tui.smart_review_cancel"), id="smart-review-cancel-edit")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "smart-review-cancel-edit":
            self.dismiss(None)
        elif event.button.id == "smart-review-save-edit":
            self._save()

    def _save(self) -> None:
        error = self.query_one("#smart-review-edit-error", Static)
        summary = self.query_one("#smart-review-summary", Input).value.strip()
        action_type = self.query_one("#smart-review-type", Input).value.strip()
        if not summary or not action_type:
            error.update(self._t("tui.smart_review_required"))
            return
        try:
            zone = ZoneInfo(self._service.config.general.timezone)
            start_raw = self.query_one("#smart-review-start", Input).value.strip()
            start = (
                datetime.strptime(start_raw, "%Y-%m-%d %H:%M").replace(tzinfo=zone)
                if start_raw
                else None
            )
            end_raw = self.query_one("#smart-review-end", Input).value.strip()
            end = (
                datetime.strptime(end_raw, "%Y-%m-%d %H:%M").replace(tzinfo=zone)
                if end_raw
                else None
            )
            for field, raw in (("due_at", start_raw), ("due_end", end_raw)):
                original = self._operation.arguments.get(field)
                if original and raw == self._time_text(original):
                    value = datetime.fromisoformat(str(original))
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=UTC)
                    if field == "due_at":
                        start = value
                    else:
                        end = value
        except ValueError:
            error.update(self._t("tui.smart_review_invalid_time"))
            return
        if end is not None and (start is None or end <= start):
            error.update(self._t("tui.smart_review_invalid_window"))
            return
        values: dict[str, Any] = {
            "summary": summary,
            "due_at": start.isoformat() if start else None,
            "due_end": end.isoformat() if end else None,
            "action_type": action_type,
            "notes": self.query_one("#smart-review-notes", TextArea).text.strip(),
            "location": self.query_one("#smart-review-location", Input).value.strip(),
            "url": self.query_one("#smart-review-url", Input).value.strip(),
        }
        if self._operation.tool == "edit_action":
            values["clear_end"] = end is None
            values["clear_due"] = start is None
        self.dismiss(values)

    def action_cancel(self) -> None:
        self.dismiss(None)
