"""Create or edit a user-owned todo item in the Actions tab."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, cast
from zoneinfo import ZoneInfo

from mailflow.domain import ActionItem
from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Select, SelectionList, Static

from mailflow_tui.action_types import ActionTypeField
from mailflow_tui.labels import error_detail
from mailflow_tui.pickers import AssistedInput
from mailflow_tui.widgets import Input, SafeSelect


class TodoCreateModal(ModalScreen[bool]):
    """Modal form for creating one user-owned todo."""

    BINDINGS: ClassVar[list[Any]] = []

    def __init__(self, service: MailFlowService, *, mail_id: str = "", summary: str = "") -> None:
        super().__init__()
        self._service = service
        self._mail_id = mail_id
        self._initial_summary = summary
        self._item: ActionItem | None = None
        self._saving = False
        self._loading = False
        self._bindings.bind("escape", "cancel", self._t("tui.btn_cancel"))
        self._bindings.bind("ctrl+s", "save", self._t("tui.btn_save"))

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        with Vertical(id="todo-create-dialog"):
            yield Static(self._t("tui.todo_add_title"), id="todo-create-title")
            with ScrollableContainer(id="todo-create-form"):
                yield Static(self._t("tui.todo_summary_label"), classes="todo-label")
                yield Input(
                    value=self._initial_summary,
                    id="todo-summary",
                    placeholder=self._t("tui.todo_summary_required"),
                )
                yield Static(self._t("tui.todo_type_label"), classes="todo-label")
                yield ActionTypeField(
                    self._service,
                    self._item.action_type if self._item else "",
                    id="todo-type",
                )
                yield Static(self._t("tui.todo_due_label"), classes="todo-label")
                yield AssistedInput(
                    self._service,
                    id="todo-due",
                    mode="datetime",
                    placeholder=self._t("tui.todo_due_placeholder"),
                )
                yield Static(self._t("tui.todo_end_label"), classes="todo-label")
                yield AssistedInput(
                    self._service,
                    id="todo-due-end",
                    mode="datetime",
                    placeholder=self._t("tui.todo_due_placeholder"),
                )
                yield Static(
                    self._t("interface.timezone", zone=self._service.config.general.timezone)
                )
                yield Static(self._t("recurrence.category"), classes="todo-label")
                yield AssistedInput(
                    self._service,
                    self._item.category if self._item else "",
                    id="todo-category",
                    mode="choice",
                )
                yield Static(self._t("recurrence.tags"), classes="todo-label")
                yield Input(value=", ".join(self._item.tags) if self._item else "", id="todo-tags")
                if self._item is not None and self._item.plan_id:
                    yield Checkbox(
                        self._t("recurrence.whole_plan"),
                        value=self._item.preview,
                        disabled=self._item.preview,
                        id="todo-whole-plan",
                    )
                    yield Checkbox(self._t("recurrence.stop_plan"), id="todo-stop-plan")
                with Vertical(id="todo-repeat-fields"):
                    yield Static(self._t("recurrence.repeat"), classes="todo-label")
                    yield SafeSelect(
                        [
                            (self._t("recurrence." + key), key)
                            for key in ("once", "daily", "weekly", "monthly")
                        ],
                        id="todo-repeat",
                        allow_blank=False,
                    )
                    yield Static(self._t("recurrence.weekdays"), id="todo-weekdays-label")
                    yield SelectionList[int](
                        *[(self._t("interface.weekday_" + str(day)), day) for day in range(7)],
                        id="todo-weekdays",
                    )
                    yield Static(self._t("recurrence.month_day"), id="todo-month-day-label")
                    yield SafeSelect(
                        [(self._t("recurrence.from_due"), "")]
                        + [(str(day), str(day)) for day in range(1, 32)],
                        id="todo-month-day",
                        allow_blank=False,
                    )
                yield Static(self._t("tui.todo_notes_label"), classes="todo-label")
                yield Input(id="todo-notes")
                yield Static(self._t("tui.todo_location_label"), classes="todo-label")
                yield Input(id="todo-location")
                yield Static(self._t("tui.todo_url_label"), classes="todo-label")
                yield Input(id="todo-url")
                yield Checkbox(
                    self._t("ux.reminder_enabled"),
                    value=self._item.reminder_enabled if self._item else True,
                    id="todo-reminder",
                )
                if self._mail_id or (self._item is not None and self._item.mail_id):
                    yield Checkbox(
                        self._t("ux.reply_task"),
                        value=self._item.reply_task if self._item else False,
                        id="todo-reply-task",
                    )
                if self._item is not None and not self._item.preview:
                    yield Checkbox(
                        self._t("recurrence.complete_delete"),
                        value=self._item.completed_at is not None,
                        id="todo-completed",
                    )
            yield Static("", id="todo-create-error")
            with Horizontal(id="todo-create-buttons"):
                yield Button(self._t("tui.btn_save"), id="todo-save", variant="primary")
                yield Button(self._t("tui.btn_cancel"), id="todo-cancel", variant="default")

    async def on_mount(self) -> None:
        self.query_one("#todo-summary", Input).focus()  # pyright: ignore[reportUnknownMemberType]
        self._repeat_visibility()
        self.run_worker(self._load_categories(), group="todo-categories", exit_on_error=False)

    async def _load_categories(self) -> None:
        try:
            items = await self._service.list_actions_all()
            categories = sorted({item.category for item in items if item.category})
            if self.is_mounted:
                field = self.query_one("#todo-category", Input).parent
                assert isinstance(field, AssistedInput)
                field.choices = [(value, value) for value in categories]
        except Exception:
            # Suggestions are optional; a custom category remains editable.
            return

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "todo-save":
            self.run_worker(self._save(), group="todo-save", exit_on_error=False)
        elif event.button.id == "todo-cancel":
            self.action_cancel()

    def action_save(self) -> None:
        self.run_worker(self._save(), group="todo-save", exit_on_error=False)

    def _repeat_visibility(self) -> None:
        whole = self._item is None or (self._item.preview if self._item else False)
        if self._item is not None and self._item.plan_id:
            whole = self.query_one("#todo-whole-plan", Checkbox).value
        self.query_one("#todo-repeat-fields").display = whole
        frequency = str(cast(Select[str], self.query_one("#todo-repeat", Select)).value)
        for key in ("weekdays", "month-day"):
            visible = frequency == ("weekly" if key == "weekdays" else "monthly")
            self.query_one(f"#todo-{key}").display = visible
            self.query_one(f"#todo-{key}-label").display = visible

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "todo-repeat":
            self._repeat_visibility()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "todo-whole-plan":
            self._repeat_visibility()

    async def _save(self) -> None:
        if self._saving or self._loading:
            return
        self._saving = True
        self.query_one("#todo-save", Button).disabled = True
        try:
            await self._save_values()
        finally:
            self._saving = False
            if self.is_mounted:
                self.query_one("#todo-save", Button).disabled = False

    async def _save_values(self) -> None:
        error = self.query_one("#todo-create-error", Static)
        summary = str(self.query_one("#todo-summary", Input).value).strip()
        action_type = self.query_one("#todo-type", ActionTypeField).value
        if not summary:
            error.update(f"[$ctp-yellow]{self._t('tui.todo_summary_required')}[/$ctp-yellow]")
            return
        due_raw = str(self.query_one("#todo-due", Input).value).strip()
        end_raw = str(self.query_one("#todo-due-end", Input).value).strip()
        try:
            local_due = datetime.strptime(due_raw, "%Y-%m-%d %H:%M") if due_raw else None
            local_end = datetime.strptime(end_raw, "%Y-%m-%d %H:%M") if end_raw else None
        except ValueError:
            error.update(f"[$ctp-yellow]{self._t('tui.todo_invalid_due')}[/$ctp-yellow]")
            return
        tz = ZoneInfo(self._service.config.general.timezone)
        due_at = local_due.replace(tzinfo=tz) if local_due else None
        due_end = local_end.replace(tzinfo=tz) if local_end is not None else None
        if self._item is not None:
            for field, raw in (("due_at", due_raw), ("due_end", end_raw)):
                original = getattr(self._item, field)
                if original is not None and raw == original.astimezone(tz).strftime(
                    "%Y-%m-%d %H:%M"
                ):
                    if field == "due_at":
                        due_at = original
                    else:
                        due_end = original
        if due_end is not None and (due_at is None or due_end <= due_at):
            error.update(f"[$ctp-yellow]{self._t('tui.todo_invalid_window')}[/$ctp-yellow]")
            return
        notes = str(self.query_one("#todo-notes", Input).value).strip()
        location = str(self.query_one("#todo-location", Input).value).strip()
        url = str(self.query_one("#todo-url", Input).value).strip()
        try:
            extra: dict[str, Any] = dict(
                category=self.query_one("#todo-category", Input).value.strip(),
                tags=[
                    tag.strip()
                    for tag in self.query_one("#todo-tags", Input)
                    .value.replace("，", ",")
                    .split(",")
                    if tag.strip()
                ],
            )
            frequency = str(cast(Select[str], self.query_one("#todo-repeat", Select)).value)
            repeat = None
            if frequency != "once":
                days = cast(
                    SelectionList[int], self.query_one("#todo-weekdays", SelectionList)
                ).selected
                month = str(cast(Select[str], self.query_one("#todo-month-day", Select)).value)
                repeat = dict(
                    frequency=frequency,
                    weekdays=days,
                    month_day=int(month) if month else None,
                )
            if self._item is not None and self._item.plan_id:
                if self.query_one("#todo-stop-plan", Checkbox).value:
                    await self._service.stop_action_plan(self._item.plan_id)
                    self.dismiss(True)
                    return
                if self.query_one("#todo-whole-plan", Checkbox).value:
                    if repeat is None:
                        error.update(self._t("recurrence.use_stop"))
                        return
                    plan = next(
                        (
                            plan
                            for plan in await self._service.action_plans()
                            if plan.plan_id == self._item.plan_id
                        ),
                        None,
                    )
                    if (
                        plan is not None
                        and plan.template.due_at is not None
                        and self._item.due_at is not None
                        and due_at is not None
                        and due_at.astimezone(tz).date() == self._item.due_at.astimezone(tz).date()
                    ):
                        # Editing a preview's time changes future occurrences,
                        # without replacing the original plan's start date.
                        duration = due_end - due_at if due_end is not None else None
                        local = due_at.astimezone(tz)
                        due_at = plan.template.due_at.astimezone(tz).replace(
                            hour=local.hour,
                            minute=local.minute,
                            second=local.second,
                            microsecond=local.microsecond,
                        )
                        due_end = due_at + duration if duration is not None else None
                    changes: dict[str, Any] = dict(
                        repeat=repeat,
                        summary=summary,
                        action_type=action_type,
                        due_at=due_at,
                        due_end=due_end,
                        notes=notes,
                        location=location,
                        url=url,
                        reminder_enabled=self.query_one("#todo-reminder", Checkbox).value,
                        **extra,
                    )
                    await self._service.update_action_plan(self._item.plan_id, **changes)
                    self.dismiss(True)
                    return
            if self._item is None:
                await self._service.add_action(
                    summary,
                    due_at,
                    action_type=action_type,
                    repeat=repeat,
                    **extra,
                    mail_id=self._mail_id,
                    reply_task=self.query_one("#todo-reply-task", Checkbox).value
                    if self._mail_id
                    else False,
                    reminder_enabled=self.query_one("#todo-reminder", Checkbox).value,
                    due_end=due_end,
                    notes=notes,
                    location=location,
                    url=url,
                )
            else:
                await self._service.edit_action(
                    self._item.item_id,
                    **extra,
                    summary=summary,
                    due_at=due_at,
                    action_type=action_type,
                    notes=notes,
                    due_end=due_end,
                    clear_end=due_end is None,
                    clear_due=due_at is None,
                    completed=self.query_one("#todo-completed", Checkbox).value,
                    reminder_enabled=self.query_one("#todo-reminder", Checkbox).value,
                    reply_task=self.query_one("#todo-reply-task", Checkbox).value
                    if self._item.mail_id
                    else False,
                    location=location,
                    url=url,
                )
        except Exception as exc:
            key = "tui.todo_create_failed" if self._item is None else "tui.todo_edit_failed"
            detail = escape(error_detail(self._service, exc))
            error.update(f"[$ctp-red]{self._t(key, error=detail)}[/$ctp-red]")
            return
        self.dismiss(True)

    def action_cancel(self) -> None:
        if not self._saving:
            self.dismiss(False)


class TodoEditModal(TodoCreateModal):
    """Modal form for editing a task."""

    def __init__(self, service: MailFlowService, item: ActionItem) -> None:
        super().__init__(service)
        self._item = item

    def compose(self) -> ComposeResult:
        yield from super().compose()

    async def on_mount(self) -> None:
        await super().on_mount()
        self._loading = True
        self.query_one("#todo-save", Button).disabled = True
        self.query_one("#todo-create-title", Static).update(self._t("tui.todo_edit_title"))
        self.query_one("#todo-create-error", Static).update(self._t("tui.loading"))
        self.run_worker(self._load_current_item(), group="todo-load", exit_on_error=False)

    async def _load_current_item(self) -> None:
        item = self._item
        assert item is not None  # set by TodoEditModal.__init__
        controls = [
            (node, node.disabled)
            for node in self.query("Input, Select, Checkbox, SelectionList, ActionTypeField")
        ]
        for node, _disabled in controls:
            node.disabled = True
        try:
            current = next(
                (
                    row
                    for row in await self._service.list_actions_all(
                        preview_days=30 if item.preview else 0
                    )
                    if row.item_id == item.item_id
                ),
                None,
            )
            if current is None:
                self.query_one("#todo-create-error", Static).update(
                    self._t("action.not_found", item_id=item.item_id)
                )
                return
            self._item = current
            self._fill_item(current)
        except Exception as exc:
            self.query_one("#todo-create-error", Static).update(error_detail(self._service, exc))
            return
        self._loading = False
        for node, disabled in controls:
            node.disabled = disabled
        self.query_one("#todo-save", Button).disabled = False
        self.query_one("#todo-create-error", Static).update("")
        self.query_one("#todo-summary", Input).focus()  # pyright: ignore[reportUnknownMemberType]

    def _fill_item(self, item: ActionItem) -> None:
        self.query_one("#todo-summary", Input).value = item.summary
        self.query_one("#todo-category", Input).value = item.category
        self.query_one("#todo-tags", Input).value = ", ".join(item.tags)
        self.query_one("#todo-reminder", Checkbox).value = item.reminder_enabled
        reply = self.query_one_optional("#todo-reply-task", Checkbox)
        if reply is not None:
            reply.value = item.reply_task
        completed = self.query_one_optional("#todo-completed", Checkbox)
        if completed is not None:
            completed.value = item.completed_at is not None
        self.query_one("#todo-due", Input).value = item.time_range_in(
            self._service.config.general.timezone
        ).split(" ~ ", 1)[0]
        self.query_one("#todo-due-end", Input).value = (
            ""
            if item.due_end is None
            else item.time_range_in(self._service.config.general.timezone).split(" ~ ", 1)[-1]
        )
        self.query_one("#todo-type", ActionTypeField).set_value(item.action_type)
        self.query_one("#todo-notes", Input).value = item.notes
        self.query_one("#todo-location", Input).value = item.location
        self.query_one("#todo-url", Input).value = item.url
        if item.repeat is not None:
            self.query_one("#todo-repeat", Select).value = item.repeat.frequency
            days = cast("SelectionList[int]", self.query_one("#todo-weekdays", SelectionList))
            days.deselect_all()
            for day in item.repeat.weekdays:
                days.select(day)
            self.query_one("#todo-month-day", Select).value = str(item.repeat.month_day or "")
        self._repeat_visibility()
