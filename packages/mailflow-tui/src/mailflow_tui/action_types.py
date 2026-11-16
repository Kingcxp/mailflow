"""Shared user-managed todo types for forms and the schedule filter."""

from __future__ import annotations

from typing import Any, ClassVar, cast

from mailflow.service import MailFlowService
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Select, SelectionList, Static

from mailflow_tui.confirm import ConfirmModal
from mailflow_tui.labels import action_type_label, error_detail
from mailflow_tui.widgets import Input, SafeSelect


class ActionTypeNameModal(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Any]] = []
    DEFAULT_CSS = """
    ActionTypeNameModal { align: center middle; }
    ActionTypeNameModal > Vertical {
        width: 60; max-width: 95%; height: auto; max-height: 90%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    ActionTypeNameModal Static, ActionTypeNameModal Horizontal { height: auto; }
    ActionTypeNameModal Button { margin-right: 1; }
    """

    def __init__(self, service: MailFlowService, name: str | None = None) -> None:
        super().__init__()
        self._service = service
        self._name = name
        self._saving = False
        self._bindings.bind("escape", "cancel", service.t("tui.btn_cancel"))
        self._bindings.bind("ctrl+s", "save", service.t("tui.btn_save"))

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                self._service.t(
                    "tui.todo_type_add" if self._name is None else "tui.todo_type_rename"
                )
            )
            yield Input(
                action_type_label(self._service, self._name) if self._name else "",
                placeholder=self._service.t("tui.todo_type_name"),
                id="action-type-name",
            )
            yield Static("", id="action-type-name-status", markup=False)
            with Horizontal():
                yield Button(
                    self._service.t("tui.btn_save"), id="action-type-save", variant="primary"
                )
                yield Button(self._service.t("tui.btn_cancel"), id="action-type-cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()  # pyright: ignore[reportUnknownMemberType]

    def action_save(self) -> None:
        if not self._saving:
            self.run_worker(self._save(), group="action-type-save", exit_on_error=False)

    async def _save(self) -> None:
        if self._saving:
            return
        self._saving = True
        self.query_one("#action-type-save", Button).disabled = True
        status = self.query_one("#action-type-name-status", Static)
        status.update(self._service.t("tui.loading"))
        try:
            name = self.query_one(Input).value.strip()
            if self._name is None:
                result = await self._service.add_action_type(name)
            else:
                if name == action_type_label(self._service, self._name):
                    name = self._name
                result = await self._service.rename_action_type(self._name, name)
        except Exception as exc:
            status.update(error_detail(self._service, exc))
        else:
            self.dismiss(result)
        finally:
            self._saving = False
            if self.is_mounted:
                self.query_one("#action-type-save", Button).disabled = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "action-type-save":
            self.action_save()
        elif event.button.id == "action-type-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        if not self._saving:
            self.dismiss(None)


class ActionTypePicker(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Any]] = []
    DEFAULT_CSS = """
    ActionTypePicker { align: center middle; }
    ActionTypePicker > Vertical {
        width: 60; max-width: 95%; height: auto;
        border: round $accent; background: $surface; padding: 1 2;
    }
    ActionTypePicker Static, ActionTypePicker Horizontal { height: auto; }
    ActionTypePicker Button { margin-right: 1; }
    """

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._bindings.bind("escape", "cancel", service.t("tui.btn_cancel"))

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._service.t("tui.batch_task_type"))
            yield ActionTypeField(self._service, id="batch-action-type")
            with Horizontal():
                yield Button(self._service.t("tui.btn_apply"), id="type-apply", variant="primary")
                yield Button(self._service.t("tui.btn_cancel"), id="type-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(
            self.query_one(ActionTypeField).value if event.button.id == "type-apply" else None
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class ActionTypesModal(ModalScreen[set[str] | None]):
    BINDINGS: ClassVar[list[Any]] = []
    DEFAULT_CSS = """
    ActionTypesModal { align: center middle; }
    ActionTypesModal > Vertical {
        width: 70; max-width: 95%; height: 85%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    ActionTypesModal Static, ActionTypesModal Horizontal { height: auto; }
    ActionTypesModal SelectionList { height: 1fr; margin: 1 0; }
    ActionTypesModal Button { margin-right: 1; }
    ActionTypesModal #action-types-status { color: $text-muted; }
    """

    def __init__(self, service: MailFlowService, selected: set[str] | None = None) -> None:
        super().__init__()
        self._service = service
        self._selected = set(selected or ())
        self._busy = True
        self._deleting = False
        self._load_failed = False
        self._bindings.bind("escape", "cancel", service.t("tui.btn_cancel"))

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._service.t("tui.todo_types_title"))
            yield Static(self._service.t("tui.todo_types_filter_hint"))
            yield SelectionList[str](id="action-types-list", disabled=True)
            yield Static(self._service.t("tui.loading"), id="action-types-status", markup=False)
            with Horizontal():
                yield Button(
                    self._service.t("tui.todo_type_add"),
                    id="action-types-add",
                    variant="success",
                    disabled=True,
                )
                yield Button(
                    self._service.t("tui.todo_type_rename"),
                    id="action-types-rename",
                    disabled=True,
                )
                yield Button(
                    self._service.t("tui.todo_type_delete"),
                    id="action-types-delete",
                    variant="error",
                    disabled=True,
                )
            with Horizontal():
                yield Button(
                    self._service.t("tui.todo_types_done"),
                    id="action-types-done",
                    variant="primary",
                    disabled=True,
                )
                yield Button(
                    self._service.t("interface.retry"), id="action-types-retry", disabled=True
                )
                yield Button(self._service.t("tui.btn_cancel"), id="action-types-cancel")

    def on_mount(self) -> None:
        self.query_one("#action-types-retry", Button).display = False
        self.call_after_refresh(self._start_load, self._selected)

    def _start_load(self, selected: set[str]) -> None:
        self.run_worker(
            self._load(selected), group="action-types-load", exclusive=True, exit_on_error=False
        )

    def _list(self) -> SelectionList[str]:
        return cast(SelectionList[str], self.query_one("#action-types-list", SelectionList))

    def _current(self) -> str | None:
        choices = self._list()
        index = choices.highlighted
        if index is None or not 0 <= index < choices.option_count:
            return None
        return choices.get_option_at_index(index).value

    def _sync_controls(self) -> None:
        current = self._current()
        self._list().disabled = self._busy
        for action in ("add", "rename", "delete", "done"):
            self.query_one(f"#action-types-{action}", Button).disabled = (
                self._busy
                or (action in {"rename", "delete"} and not current)
                or (action == "done" and not self._list().selected)
            )
        retry = self.query_one("#action-types-retry", Button)
        retry.display = self._load_failed
        retry.disabled = self._busy

    async def _load(self, selected: set[str]) -> None:
        self._busy = True
        try:
            self._sync_controls()
            self.query_one("#action-types-status", Static).update(self._service.t("tui.loading"))
            names = await self._service.list_action_types()
            if not self.is_attached:
                return
            choices = self._list()
            choices.clear_options()
            values = [*names, ""]
            choices.add_options(
                (Text(action_type_label(self._service, name)), name, name in selected)
                for name in values
            )
            self._load_failed = False
            self.query_one("#action-types-status", Static).update(
                "" if names else self._service.t("tui.todo_types_empty")
            )
        except Exception as exc:
            self._load_failed = True
            if self.is_attached:
                self.query_one("#action-types-status", Static).update(
                    error_detail(self._service, exc)
                )
        finally:
            self._busy = False
            if self.is_attached:
                self._sync_controls()

    def on_selection_list_selection_highlighted(
        self, event: SelectionList.SelectionHighlighted[str]
    ) -> None:
        self._sync_controls()

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged[str]) -> None:
        self._sync_controls()

    def _edit_name(self, name: str | None) -> None:
        selected = set(self._list().selected)

        def closed(result: str | None) -> None:
            if result is None:
                return
            next_selected = set(selected)
            if name is not None and name in next_selected:
                next_selected.remove(name)
                next_selected.add(result)
            self._start_load(next_selected)

        cast(Any, self.app).push_screen(ActionTypeNameModal(self._service, name), closed)

    async def _delete(self, name: str) -> None:
        if self._busy:
            return
        self._busy = True
        self._deleting = True
        self._sync_controls()
        try:
            confirmed = await cast(Any, self.app).push_screen_wait(
                ConfirmModal(
                    self._service,
                    title=self._service.t("tui.todo_type_delete"),
                    body=self._service.t(
                        "tui.todo_type_delete_confirm", name=action_type_label(self._service, name)
                    ),
                    confirm_label=self._service.t("tui.todo_type_delete"),
                    variant="error",
                    focus_confirm=False,
                )
            )
            if confirmed:
                selected = set(self._list().selected) - {name}
                if not await self._service.delete_action_type(name):
                    raise ValueError(self._service.t("action.type_not_found", name=name))
                await self._load(selected)
        except Exception as exc:
            self.query_one("#action-types-status", Static).update(error_detail(self._service, exc))
        finally:
            self._busy = False
            self._deleting = False
            if self.is_mounted:
                self._sync_controls()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        ident = event.button.id
        if ident == "action-types-cancel":
            self.action_cancel()
        elif not self._busy:
            if ident == "action-types-done":
                self.dismiss(set(self._list().selected))
            elif ident == "action-types-retry":
                self._start_load(set(self._list().selected))
            elif ident == "action-types-add":
                self._edit_name(None)
            elif ident == "action-types-rename":
                name = self._current()
                if name:
                    self._edit_name(name)
            elif ident == "action-types-delete":
                name = self._current()
                if name:
                    self.run_worker(
                        self._delete(name), group="action-types-delete", exit_on_error=False
                    )

    def action_cancel(self) -> None:
        if not self._deleting:
            self.dismiss(None)


class ActionTypeField(Horizontal):
    """Choose or create a type without losing the todo form."""

    DEFAULT_CSS = """
    ActionTypeField { height: auto; align-vertical: middle; margin-bottom: 1; }
    ActionTypeField Select { width: 1fr; margin-bottom: 0; }
    ActionTypeField Button { width: auto; margin-left: 1; }
    """

    def __init__(self, service: MailFlowService, value: str = "", *, id: str) -> None:
        super().__init__(id=id)
        self._service = service
        self.value = value
        self._names: list[str] = []

    def _options(self) -> list[tuple[Text, str]]:
        names = list(dict.fromkeys(["", *self._names, self.value]))
        return [(Text(action_type_label(self._service, name)), name) for name in names]

    def compose(self) -> ComposeResult:
        yield SafeSelect(self._options(), value=self.value, allow_blank=False)
        yield Button(self._service.t("tui.todo_type_add"), variant="success")

    def on_mount(self) -> None:
        self.call_after_refresh(
            self.run_worker, self._load, group="action-type-options", exit_on_error=False
        )

    async def _load(self) -> None:
        try:
            names = await self._service.list_action_types()
            if self.is_mounted:
                self._names = names
                self.set_value(self.value)
        except Exception as exc:
            if self.is_mounted:
                self.notify(error_detail(self._service, exc), severity="error")

    def set_value(self, value: str) -> None:
        self.value = value
        if self.is_mounted:
            select = cast(Select[str], self.query_one(Select))
            with select.prevent(Select.Changed):
                select.set_options(self._options())
                select.value = value

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        if event.value is not Select.NULL:
            self.value = str(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()

        def closed(result: str | None) -> None:
            if result is not None:
                self.set_value(result)
            self.run_worker(self._load(), group="action-type-options", exit_on_error=False)

        cast(Any, self.app).push_screen(ActionTypeNameModal(self._service), closed)
