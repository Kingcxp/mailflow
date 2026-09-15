"""Explicit review UI for LLM-discovered seminar proposals."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mailflow.domain import SeminarCandidate, SeminarStatus
from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static, TextArea

from mailflow_tui.labels import error_detail


class SeminarReviewModal(ModalScreen[bool]):
    """Present each proposal as an editable form before schedule import."""

    BINDINGS: ClassVar[list[Any]] = []

    def __init__(self, service: MailFlowService, candidates: list[SeminarCandidate]) -> None:
        super().__init__()
        self._service = service
        self._candidates = list(candidates)
        self._changed = False
        self._bindings.bind("escape", "cancel", self._t("tui.seminar_cancel"))

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    @property
    def _candidate(self) -> SeminarCandidate:
        return self._candidates[0]

    def _format_time(self, value: datetime | None, timezone: str) -> str:
        if value is None:
            return ""
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            zone = ZoneInfo(self._service.config.general.timezone)
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(zone).strftime("%Y-%m-%d %H:%M")

    def compose(self) -> ComposeResult:
        candidate = self._candidate
        with Vertical(id="seminar-review-dialog"):
            yield Static(self._t("tui.seminar_review_title"), id="seminar-review-title")
            with ScrollableContainer(id="seminar-review-form"):
                yield Static("", id="seminar-source")
                yield Static("", id="seminar-confidence")
                yield Static("", id="seminar-expired")
                yield Static(self._t("tui.seminar_title_label"), classes="todo-label")
                yield Input(value=candidate.title, id="seminar-title")
                yield Static(self._t("tui.seminar_start_label"), classes="todo-label")
                yield Input(
                    value=self._format_time(candidate.starts_at, candidate.timezone),
                    id="seminar-start",
                )
                yield Static(self._t("tui.seminar_end_label"), classes="todo-label")
                yield Input(
                    value=self._format_time(candidate.ends_at, candidate.timezone),
                    id="seminar-end",
                )
                yield Static(self._t("tui.seminar_timezone_label"), classes="todo-label")
                yield Input(value=candidate.timezone, id="seminar-timezone")
                yield Static(self._t("tui.seminar_location_label"), classes="todo-label")
                yield Input(value=candidate.location, id="seminar-location")
                yield Static(self._t("tui.seminar_url_label"), classes="todo-label")
                yield Input(value=candidate.url, id="seminar-url")
                yield Static(self._t("tui.seminar_description_label"), classes="todo-label")
                yield TextArea(candidate.description, id="seminar-description")
                yield Static("", id="seminar-evidence")
                yield Static("", id="seminar-review-error")
            with Horizontal(id="seminar-review-buttons"):
                yield Button(self._t("tui.seminar_import"), id="seminar-import", variant="primary")
                yield Button(self._t("tui.seminar_reject"), id="seminar-reject", variant="error")
                yield Button(self._t("tui.seminar_cancel"), id="seminar-cancel")

    async def on_mount(self) -> None:
        self._render_candidate()
        self.query_one("#seminar-title", Input).focus()  # pyright: ignore[reportUnknownMemberType]

    def _render_candidate(self) -> None:
        candidate = self._candidate
        self.query_one("#seminar-review-title", Static).update(self._t("tui.seminar_review_title"))
        self.query_one("#seminar-source", Static).update(
            "[bold]" + escape(self._t("tui.seminar_source", mail_id=candidate.mail_id)) + "[/bold]"
        )
        self.query_one("#seminar-confidence", Static).update(
            self._t("tui.seminar_confidence", confidence=candidate.confidence)
        )
        expired = candidate.status is SeminarStatus.EXPIRED
        self.query_one("#seminar-expired", Static).update(
            f"[yellow]{self._t('tui.seminar_expired')}[/yellow]" if expired else ""
        )
        self.query_one("#seminar-evidence", Static).update(
            f"[bold]{self._t('tui.seminar_evidence_label')}:[/bold] "
            f"{escape(candidate.evidence or '-')}"
        )

    def _input_text(self, selector: str) -> str:
        return str(self.query_one(selector, Input).value).strip()

    def _show_error(self, message: str) -> None:
        self.query_one("#seminar-review-error", Static).update(f"[red]{escape(message)}[/red]")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "seminar-import":
            await self._import()
        elif event.button.id == "seminar-reject":
            await self._reject()
        elif event.button.id == "seminar-cancel":
            self.dismiss(self._changed)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in {
            "seminar-title",
            "seminar-start",
            "seminar-end",
            "seminar-timezone",
            "seminar-location",
            "seminar-url",
        }:
            await self._import()

    async def _import(self) -> None:
        start_raw = self._input_text("#seminar-start")
        end_raw = self._input_text("#seminar-end")
        try:
            starts_at = datetime.strptime(start_raw, "%Y-%m-%d %H:%M")
            ends_at = datetime.strptime(end_raw, "%Y-%m-%d %H:%M") if end_raw else None
        except ValueError:
            self._show_error(self._t("tui.seminar_invalid_time"))
            return
        try:
            await self._service.import_seminar(
                self._candidate.candidate_id,
                title=self._input_text("#seminar-title"),
                starts_at=starts_at,
                ends_at=ends_at,
                clear_end=not end_raw,
                timezone=self._input_text("#seminar-timezone"),
                location=self._input_text("#seminar-location"),
                url=self._input_text("#seminar-url"),
                description=self.query_one("#seminar-description", TextArea).text.strip(),
            )
        except Exception as exc:
            self._show_error(
                self._t("tui.seminar_import_failed", error=error_detail(self._service, exc))
            )
            return
        self._changed = True
        self.notify(self._t("tui.seminar_imported"), timeout=4)
        self._advance()

    async def _reject(self) -> None:
        try:
            rejected = await self._service.reject_seminar(self._candidate.candidate_id)
        except Exception as exc:
            self._show_error(
                self._t("tui.seminar_reject_failed", error=error_detail(self._service, exc))
            )
            return
        if not rejected:
            self._show_error(self._t("seminar.candidate_not_found"))
            return
        self._changed = True
        self.notify(self._t("tui.seminar_rejected"), timeout=4)
        self._advance()

    def _advance(self) -> None:
        self._candidates.pop(0)
        if not self._candidates:
            self.dismiss(self._changed)
            return
        candidate = self._candidate
        self.query_one("#seminar-title", Input).value = candidate.title
        self.query_one("#seminar-start", Input).value = self._format_time(
            candidate.starts_at, candidate.timezone
        )
        self.query_one("#seminar-end", Input).value = self._format_time(
            candidate.ends_at, candidate.timezone
        )
        self.query_one("#seminar-timezone", Input).value = candidate.timezone
        self.query_one("#seminar-location", Input).value = candidate.location
        self.query_one("#seminar-url", Input).value = candidate.url
        self.query_one("#seminar-description", TextArea).text = candidate.description
        self.query_one("#seminar-review-error", Static).update("")
        self._render_candidate()

    def action_cancel(self) -> None:
        self.dismiss(self._changed)
