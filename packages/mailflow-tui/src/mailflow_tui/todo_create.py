"""Create or edit a user-owned todo item in the Actions tab."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from mailflow.domain import ActionItem
from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from mailflow_tui.labels import error_detail


class TodoCreateModal(ModalScreen[bool]):
    """Modal form for creating one user-owned todo."""

    BINDINGS: ClassVar[list[Any]] = []

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._item: ActionItem | None = None
        self._bindings.bind("escape", "cancel", self._t("tui.btn_cancel"))

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        with Vertical(id="todo-create-dialog"):
            yield Static(self._t("tui.todo_add_title"), id="todo-create-title")
            with Vertical(id="todo-create-form"):
                yield Static(self._t("tui.todo_summary_label"), classes="todo-label")
                yield Input(id="todo-summary", placeholder=self._t("tui.todo_summary_required"))
                yield Static(self._t("tui.todo_type_label"), classes="todo-label")
                yield Input(
                    value="errand", id="todo-type", placeholder=self._t("tui.todo_type_placeholder")
                )
                yield Static(self._t("tui.todo_due_label"), classes="todo-label")
                yield Input(id="todo-due", placeholder=self._t("tui.todo_due_placeholder"))
                yield Static(self._t("tui.todo_end_label"), classes="todo-label")
                yield Input(id="todo-due-end", placeholder=self._t("tui.todo_due_placeholder"))
                yield Static(self._t("tui.todo_notes_label"), classes="todo-label")
                yield Input(id="todo-notes")
                yield Static(self._t("tui.todo_location_label"), classes="todo-label")
                yield Input(id="todo-location")
                yield Static(self._t("tui.todo_url_label"), classes="todo-label")
                yield Input(id="todo-url")
                yield Static("", id="todo-create-error")
            with Horizontal(id="todo-create-buttons"):
                yield Button(self._t("tui.btn_save"), id="todo-save", variant="primary")
                yield Button(self._t("tui.btn_cancel"), id="todo-cancel", variant="default")

    async def on_mount(self) -> None:
        self.query_one("#todo-summary", Input).focus()  # pyright: ignore[reportUnknownMemberType]

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "todo-save":
            await self._save()
        elif event.button.id == "todo-cancel":
            self.dismiss(False)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in (
            "todo-summary",
            "todo-type",
            "todo-due",
            "todo-due-end",
            "todo-notes",
            "todo-location",
            "todo-url",
        ):
            await self._save()

    async def _save(self) -> None:
        error = self.query_one("#todo-create-error", Static)
        summary = str(self.query_one("#todo-summary", Input).value).strip()
        action_type = str(self.query_one("#todo-type", Input).value).strip()
        if not summary:
            error.update(f"[yellow]{self._t('tui.todo_summary_required')}[/yellow]")
            return
        if not action_type:
            error.update(f"[yellow]{self._t('tui.todo_type_required')}[/yellow]")
            return
        due_raw = str(self.query_one("#todo-due", Input).value).strip()
        end_raw = str(self.query_one("#todo-due-end", Input).value).strip()
        try:
            local_due = datetime.strptime(due_raw, "%Y-%m-%d %H:%M")
            local_end = datetime.strptime(end_raw, "%Y-%m-%d %H:%M") if end_raw else None
        except ValueError:
            error.update(f"[yellow]{self._t('tui.todo_invalid_due')}[/yellow]")
            return
        tz = ZoneInfo(self._service.config.general.timezone)
        due_at = local_due.replace(tzinfo=tz)
        due_end = local_end.replace(tzinfo=tz) if local_end is not None else None
        if due_end is not None and due_end <= due_at:
            error.update(f"[yellow]{self._t('tui.todo_invalid_window')}[/yellow]")
            return
        notes = str(self.query_one("#todo-notes", Input).value).strip()
        location = str(self.query_one("#todo-location", Input).value).strip()
        url = str(self.query_one("#todo-url", Input).value).strip()
        try:
            if self._item is None:
                await self._service.add_action(
                    summary,
                    due_at,
                    action_type=action_type,
                    due_end=due_end,
                    notes=notes,
                    location=location,
                    url=url,
                )
            else:
                await self._service.edit_action(
                    self._item.item_id,
                    summary=summary,
                    due_at=due_at,
                    action_type=action_type,
                    notes=notes,
                    due_end=due_end,
                    clear_end=due_end is None,
                    location=location,
                    url=url,
                )
        except Exception as exc:
            key = "tui.todo_create_failed" if self._item is None else "tui.todo_edit_failed"
            detail = escape(error_detail(self._service, exc))
            error.update(f"[red]{self._t(key, error=detail)}[/red]")
            return
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class TodoEditModal(TodoCreateModal):
    """Modal form for editing a custom todo or imported seminar."""

    def __init__(self, service: MailFlowService, item: ActionItem) -> None:
        super().__init__(service)
        self._item = item

    def compose(self) -> ComposeResult:
        yield from super().compose()

    async def on_mount(self) -> None:
        await super().on_mount()
        item = self._item
        assert item is not None  # set by TodoEditModal.__init__
        self.query_one("#todo-create-title", Static).update(self._t("tui.todo_edit_title"))
        self.query_one("#todo-summary", Input).value = item.summary
        self.query_one("#todo-due", Input).value = item.time_range_in(
            self._service.config.general.timezone
        ).split(" ~ ", 1)[0]
        self.query_one("#todo-due-end", Input).value = (
            ""
            if item.due_end is None
            else item.time_range_in(self._service.config.general.timezone).split(" ~ ", 1)[-1]
        )
        self.query_one("#todo-type", Input).value = item.action_type
