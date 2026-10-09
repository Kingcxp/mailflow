"""Editable review surface for staged smart-action operations.

Nothing in this modal calls a mutating service method. It edits private copies
of pending operations and returns those copies only when the user selects Apply.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from mailflow.domain import PendingOperation, SeminarCandidate
from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static, TextArea


class SmartActionReviewModal(ModalScreen[list[PendingOperation] | None]):
    BINDINGS: ClassVar[list[Any]] = []

    def __init__(
        self,
        service: MailFlowService,
        operations: list[PendingOperation],
        seminar_candidates: dict[str, SeminarCandidate],
    ) -> None:
        super().__init__()
        self._service = service
        self._operations = [operation.model_copy(deep=True) for operation in operations]
        self._seminar_candidates = dict(seminar_candidates)
        self._labels: dict[int, Static] = {}
        self._bindings.bind("escape", "cancel", self._t("tui.smart_review_cancel"))

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def _local_time(self, value: Any) -> str:
        if not value:
            return ""
        try:
            moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            return moment.astimezone(ZoneInfo(self._service.config.general.timezone)).strftime(
                "%Y-%m-%d %H:%M"
            )
        except (TypeError, ValueError):
            return str(value)

    def _description(self, index: int, operation: PendingOperation) -> str:
        args = operation.arguments
        candidate = self._seminar_candidates.get(str(args.get("candidate_id") or ""))
        title = str(
            args.get("summary") or args.get("title") or (candidate.title if candidate else "")
        )
        action_type = str(args.get("action_type") or "")
        start = (
            args.get("due_at")
            or args.get("starts_at")
            or (candidate.starts_at if candidate else None)
        )
        end = (
            args.get("due_end") or args.get("ends_at") or (candidate.ends_at if candidate else None)
        )
        notes = str(
            args.get("notes")
            or args.get("description")
            or (candidate.description if candidate else "")
        )
        location = str(args.get("location") or (candidate.location if candidate else ""))
        url = str(args.get("url") or (candidate.url if candidate else ""))
        parts = [self._t(f"tui.smart_review_operation_{operation.tool}"), title]
        if action_type:
            parts.append(action_type)
        if start:
            parts.append(self._local_time(start))
        if end:
            parts.append(self._local_time(end))
        if location:
            parts.append(location)
        if url:
            parts.append(url)
        if notes:
            parts.append(notes)
        if operation.tool == "delete_action":
            key = (
                "tui.smart_review_delete_analysis"
                if args.get("origin") == "analysis"
                else "tui.smart_review_delete_item"
            )
            parts.append(self._t(key))
        if operation.tool == "edit_action" and args.get("origin") == "analysis":
            parts.append(self._t("tui.smart_review_analysis_readonly"))
        return " · ".join(part for part in parts if part)

    def _can_edit(self, operation: PendingOperation) -> bool:
        if operation.tool not in {
            "add_action",
            "schedule_event",
            "edit_action",
            "schedule_seminar",
        }:
            return False
        return not (
            operation.tool == "edit_action" and operation.arguments.get("origin") == "analysis"
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="smart-review-dialog"):
            yield Static(self._t("tui.smart_review_title"), id="smart-review-title")
            with ScrollableContainer(id="smart-review-operations"):
                for index, operation in enumerate(self._operations):
                    with Horizontal(classes="smart-review-row"):
                        label = Static(
                            self._description(index, operation), id=f"smart-review-op-{index}"
                        )
                        self._labels[index] = label
                        yield label
                        if self._can_edit(operation):
                            yield Button(
                                self._t("tui.smart_review_edit"),
                                id=f"smart-review-edit-{index}",
                                variant="primary",
                            )
            yield Static(self._t("tui.smart_review_apply_notice"), id="smart-review-notice")
            yield Static("", id="smart-review-error")
            with Horizontal(id="smart-review-buttons"):
                yield Button(
                    self._t("tui.smart_review_apply"), id="smart-review-apply", variant="success"
                )
                yield Button(self._t("tui.smart_review_cancel"), id="smart-review-cancel")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "smart-review-apply":
            self.dismiss([operation.model_copy(deep=True) for operation in self._operations])
        elif button_id == "smart-review-cancel":
            self.dismiss(None)
        elif button_id.startswith("smart-review-edit-"):
            try:
                index = int(button_id.rsplit("-", 1)[1])
            except ValueError:
                return
            if not 0 <= index < len(self._operations):
                return
            operation = self._operations[index]
            self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
                SmartOperationEditModal(self._service, operation, self._seminar_candidates),
                lambda values: self._updated(index, values),
            )

    def _updated(self, index: int, values: dict[str, Any] | None) -> None:
        if values is None or not 0 <= index < len(self._operations):
            return
        operation = self._operations[index]
        operation.arguments = {**operation.arguments, **values}
        label = self._labels.get(index)
        if label is not None:
            label.update(self._description(index, operation))
        error = self.query_one_optional("#smart-review-error", Static)
        if error is not None:
            error.update("")

    def action_cancel(self) -> None:
        self.dismiss(None)


class SmartOperationEditModal(ModalScreen[dict[str, Any] | None]):
    BINDINGS: ClassVar[list[Any]] = []

    def __init__(
        self,
        service: MailFlowService,
        operation: PendingOperation,
        candidates: dict[str, SeminarCandidate],
    ) -> None:
        super().__init__()
        self._service = service
        self._operation = operation.model_copy(deep=True)
        self._candidate = candidates.get(str(operation.arguments.get("candidate_id") or ""))
        self._bindings.bind("escape", "cancel", self._t("tui.smart_review_cancel"))

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def _time_text(self, raw: Any) -> str:
        if not raw:
            return ""
        try:
            value = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.astimezone(ZoneInfo(self._service.config.general.timezone)).strftime(
                "%Y-%m-%d %H:%M"
            )
        except (TypeError, ValueError):
            return ""

    def compose(self) -> ComposeResult:
        args = self._operation.arguments
        candidate = self._candidate
        seminar = self._operation.tool == "schedule_seminar"
        title = args.get("summary") or args.get("title") or (candidate.title if candidate else "")
        start = (
            args.get("due_at")
            or args.get("starts_at")
            or (candidate.starts_at if candidate else None)
        )
        end = (
            args.get("due_end") or args.get("ends_at") or (candidate.ends_at if candidate else None)
        )
        notes = (
            args.get("notes")
            or args.get("description")
            or (candidate.description if candidate else "")
        )
        with Vertical(id="smart-review-edit-dialog"):
            yield Static(self._t("tui.smart_review_edit_title"), id="smart-review-edit-title")
            with ScrollableContainer(id="smart-review-edit-fields"):
                yield Static(self._t("tui.smart_review_summary"), classes="todo-label")
                yield Input(value=str(title or ""), id="smart-review-summary")
                if not seminar:
                    yield Static(self._t("tui.smart_review_action_type"), classes="todo-label")
                    yield Input(
                        value=str(args.get("action_type") or "errand"), id="smart-review-type"
                    )
                yield Static(self._t("tui.smart_review_start"), classes="todo-label")
                yield Input(value=self._time_text(start), id="smart-review-start")
                yield Static(self._t("tui.smart_review_end"), classes="todo-label")
                yield Input(value=self._time_text(end), id="smart-review-end")
                if seminar:
                    yield Static(self._t("tui.seminar_timezone_label"), classes="todo-label")
                    yield Input(
                        value=str(
                            args.get("timezone")
                            or (
                                candidate.timezone
                                if candidate
                                else self._service.config.general.timezone
                            )
                        ),
                        id="smart-review-timezone",
                    )
                yield Static(self._t("tui.smart_review_notes"), classes="todo-label")
                yield TextArea(str(notes or ""), id="smart-review-notes")
                yield Static(self._t("tui.smart_review_location"), classes="todo-label")
                yield Input(
                    value=str(args.get("location") or (candidate.location if candidate else "")),
                    id="smart-review-location",
                )
                yield Static(self._t("tui.smart_review_url"), classes="todo-label")
                yield Input(
                    value=str(args.get("url") or (candidate.url if candidate else "")),
                    id="smart-review-url",
                )
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
        summary = str(self.query_one("#smart-review-summary", Input).value).strip()
        is_seminar = self._operation.tool == "schedule_seminar"
        action_type = (
            str(self.query_one("#smart-review-type", Input).value).strip() if not is_seminar else ""
        )
        if not summary or (not is_seminar and not action_type):
            error.update(f"[yellow]{self._t('tui.smart_review_required')}[/yellow]")
            return
        start_raw = str(self.query_one("#smart-review-start", Input).value).strip()
        end_raw = str(self.query_one("#smart-review-end", Input).value).strip()
        try:
            zone = ZoneInfo(self._service.config.general.timezone)
            start = datetime.strptime(start_raw, "%Y-%m-%d %H:%M").replace(tzinfo=zone)
            end = (
                datetime.strptime(end_raw, "%Y-%m-%d %H:%M").replace(tzinfo=zone)
                if end_raw
                else None
            )
        except ValueError:
            error.update(f"[yellow]{self._t('tui.smart_review_invalid_time')}[/yellow]")
            return
        if end is not None and end <= start:
            error.update(f"[yellow]{self._t('tui.smart_review_invalid_window')}[/yellow]")
            return
        notes = self.query_one("#smart-review-notes", TextArea).text.strip()
        location = str(self.query_one("#smart-review-location", Input).value).strip()
        url = str(self.query_one("#smart-review-url", Input).value).strip()
        if is_seminar:
            values: dict[str, Any] = {
                "title": summary,
                "starts_at": start.isoformat(),
                "ends_at": end.isoformat() if end else None,
                "clear_end": end is None,
                "timezone": str(self.query_one("#smart-review-timezone", Input).value).strip(),
                "description": notes,
                "location": location,
                "url": url,
            }
        elif self._operation.tool == "schedule_event":
            values = {
                "title": summary,
                "starts_at": start.isoformat(),
                "ends_at": end.isoformat() if end else None,
                "action_type": action_type,
                "notes": notes,
                "location": location,
                "url": url,
            }
        elif self._operation.tool == "edit_action":
            values = {
                "summary": summary,
                "due_at": start.isoformat(),
                "due_end": end.isoformat() if end else None,
                "clear_end": end is None,
                "action_type": action_type,
                "notes": notes,
                "location": location,
                "url": url,
            }
        else:
            values = {
                "summary": summary,
                "due_at": start.isoformat(),
                "due_end": end.isoformat() if end else None,
                "action_type": action_type,
                "notes": notes,
                "location": location,
                "url": url,
            }
        self.dismiss(values)

    def action_cancel(self) -> None:
        self.dismiss(None)
