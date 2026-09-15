"""Create or edit a user-owned todo item in the Actions tab."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, cast
from zoneinfo import ZoneInfo

from mailflow.domain import ActionItem
from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

_ACTION_TYPES: tuple[tuple[str, str], ...] = (
    ("tui.action_type_errand", "errand"),
    ("tui.action_type_exam", "exam"),
    ("tui.action_type_meeting", "meeting"),
    ("tui.action_type_other", "other"),
)


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
                yield Select(
                    [(self._t(key), value) for key, value in _ACTION_TYPES],
                    value="errand",
                    id="todo-type",
                    allow_blank=False,
                )
                yield Static(self._t("tui.todo_due_label"), classes="todo-label")
                yield Input(id="todo-due", placeholder=self._t("tui.todo_due_placeholder"))
                yield Static(self._t("tui.todo_notes_label"), classes="todo-label")
                yield Input(id="todo-notes")
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
        if event.input.id in ("todo-summary", "todo-due", "todo-notes"):
            await self._save()

    async def _save(self) -> None:
        error = self.query_one("#todo-create-error", Static)
        summary = str(self.query_one("#todo-summary", Input).value).strip()
        if not summary:
            error.update(f"[yellow]{self._t('tui.todo_summary_required')}[/yellow]")
            return
        due_raw = str(self.query_one("#todo-due", Input).value).strip()
        try:
            local_due = datetime.strptime(due_raw, "%Y-%m-%d %H:%M")
        except ValueError:
            error.update(f"[yellow]{self._t('tui.todo_invalid_due')}[/yellow]")
            return
        tz = ZoneInfo(self._service.config.general.timezone)
        due_at = local_due.replace(tzinfo=tz)
        type_select = self.query_one("#todo-type", Select)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        raw_value = type_select.value  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        action_type = str(raw_value) if raw_value is not Select.NULL else "errand"  # pyright: ignore[reportUnknownArgumentType]
        notes = str(self.query_one("#todo-notes", Input).value).strip()
        try:
            if self._item is None:
                await self._service.add_action(
                    summary, due_at, action_type=action_type, notes=notes
                )
            else:
                await self._service.edit_action(
                    self._item.item_id,
                    summary=summary,
                    due_at=due_at,
                    action_type=action_type,
                    notes=notes,
                )
        except Exception as exc:
            key = "tui.todo_create_failed" if self._item is None else "tui.todo_edit_failed"
            error.update(f"[red]{self._t(key, error=str(exc))}[/red]")
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
        self.query_one("#todo-notes", Input).value = item.notes
        select = cast("Select[str]", self.query_one("#todo-type", Select))
        if item.action_type in {value for _key, value in _ACTION_TYPES}:
            select.value = item.action_type
