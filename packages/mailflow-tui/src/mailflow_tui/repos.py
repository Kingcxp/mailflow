"""Remote repository manager: list, add and remove plugin marketplace
connections from the TUI (the same surface as `plugin repo add|remove`)."""

from __future__ import annotations

from typing import Any, ClassVar

from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static

from mailflow_tui.labels import error_message
from mailflow_tui.view_state import TableView, update_table
from mailflow_tui.widgets import Input


class ReposScreen(ModalScreen[bool | None]):
    """Manage remote plugin repositories (add / remove connections)."""

    BINDINGS: ClassVar[list[Any]] = []

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._editing_name: str | None = None
        self._busy = False
        self._status_by_name: dict[str, str] = {}
        self._bindings.bind("escape", "dismiss_modal", self._t("tui.btn_cancel"))

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        with Vertical(id="repos-dialog"):
            yield Static(self._t("tui.repos_title"), classes="scaffold-title")
            yield DataTable(id="repos-table", cursor_type="row")
            with Vertical(id="repos-form"):
                yield Input(placeholder=self._t("tui.repos_name"), id="repos-name")
                yield Input(placeholder=self._t("tui.repos_url"), id="repos-url")
            with Horizontal(id="repos-actions"):
                yield Button(self._t("tui.btn_add"), id="repos-add", variant="success")
                yield Button(self._t("tui.btn_remove"), id="repos-remove", variant="warning")
                yield Button(self._t("tui.btn_back"), id="repos-cancel", variant="default")
            yield Static(self._t("tui.repos_esc_hint"), id="repos-esc-hint")

    def action_dismiss_modal(self) -> None:
        if not self._busy:
            self.dismiss(None)

    def on_mount(self) -> None:
        table: DataTable[Any] = self.query_one("#repos-table", DataTable)  # pyright: ignore[reportUnknownVariableType]
        table.add_column(self._t("plugin.header_name"), key="name")  # pyright: ignore[reportUnknownMemberType]
        table.add_column(self._t("tui.repos_url_header"), key="url")  # pyright: ignore[reportUnknownMemberType]
        table.add_column(self._t("tui.repos_status_header"), key="status")  # pyright: ignore[reportUnknownMemberType]
        self._reload()
        self.run_worker(
            self._refresh_status(), exclusive=True, group="repo-status", exit_on_error=False
        )

    def _reload(self) -> None:
        table: DataTable[Any] = self.query_one("#repos-table", DataTable)  # pyright: ignore[reportUnknownVariableType]
        view = TableView.capture(table)
        checking = self._t("tui.repo_status_checking")
        update_table(
            table,
            [
                (repo.name, (repo.name, repo.url, self._status_by_name.get(repo.name, checking)))
                for repo in self._service.config.plugins.repositories
            ],
        )
        view.restore(table)
        self.query_one("#repos-remove", Button).disabled = self._busy or not table.row_count

    async def _refresh_status(self) -> None:
        try:
            statuses = await self._service.market_repository_statuses()
        except Exception as exc:
            message = error_message(self._service, exc)
            self._status_by_name = {
                repo.name: self._t("tui.repo_status_failed", error=message)
                for repo in self._service.config.plugins.repositories
            }
        else:
            self._status_by_name = {
                status.repository.name: (
                    self._t("tui.repo_status_ok", count=status.plugin_count)
                    if status.ok
                    else self._t("tui.repo_status_failed", error=self._t(status.error_code))
                )
                for status in statuses
            }
        self._reload()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "repos-cancel":
            self.action_dismiss_modal()
            return
        if self._busy:
            return
        if button_id == "repos-add":
            self._busy = True
            self.run_worker(self._operate(remove=False), exit_on_error=False)
            return
        if button_id == "repos-remove":
            self._busy = True
            self.run_worker(self._operate(remove=True), exit_on_error=False)

    async def _operate(self, *, remove: bool) -> None:
        self.query_one("#repos-esc-hint", Static).update(self._t("tui.form_saving"))
        for control in self.query("Button, Input"):
            control.disabled = True
        try:
            if remove:
                await self._remove_repo()
            else:
                await self._add_repo()
        except Exception as exc:
            self.query_one("#repos-esc-hint", Static).update(error_message(self._service, exc))
        finally:
            self._busy = False
            if self.is_mounted:
                for control in self.query("Button, Input"):
                    control.disabled = False
                self.query_one("#repos-remove", Button).disabled = not self.query_one(
                    "#repos-table", DataTable
                ).row_count

    def on_data_table_row_selected(self, event: Any) -> None:
        """Double-click / Enter on a repo loads it into the form for editing."""
        if self._busy:
            return
        table: DataTable[Any] = self.query_one("#repos-table", DataTable)  # pyright: ignore[reportUnknownVariableType]
        try:
            row_values = table.get_row_at(table.cursor_row)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        except Exception:
            return
        name = str(row_values[0])
        self._editing_name = name
        self.query_one("#repos-name", Input).value = name
        self.query_one("#repos-url", Input).value = str(row_values[1])
        add_button = self.query_one("#repos-add", Button)
        add_button.label = self._t("tui.repos_update")
        add_button.variant = "primary"

    def _reset_edit_state(self) -> None:
        self._editing_name = None
        self.query_one("#repos-name", Input).value = ""
        self.query_one("#repos-url", Input).value = ""
        add_button = self.query_one("#repos-add", Button)
        add_button.label = self._t("tui.btn_add")
        add_button.variant = "success"

    async def _add_repo(self) -> None:
        name = self.query_one("#repos-name", Input).value.strip()
        url = self.query_one("#repos-url", Input).value.strip()
        if not name or not url:
            self.query_one("#repos-esc-hint", Static).update(self._t("tui.repos_missing_fields"))
            return
        editing = self._editing_name
        try:
            if editing is not None:
                await self._service.plugin_repo_edit(editing, name, url)
            else:
                await self._service.plugin_repo_add(name, url)
        except (KeyError, ValueError) as exc:
            self.query_one("#repos-esc-hint", Static).update(error_message(self._service, exc))
            return
        self._reset_edit_state()
        self._reload()
        self.run_worker(
            self._refresh_status(), exclusive=True, group="repo-status", exit_on_error=False
        )
        self.query_one("#repos-esc-hint", Static).update(self._t("plugin.repo_added", name=name))

    async def _remove_repo(self) -> None:
        table: DataTable[Any] = self.query_one("#repos-table", DataTable)  # pyright: ignore[reportUnknownVariableType]
        row_index = table.cursor_row
        if row_index < 0 or not table.row_count:
            self.query_one("#repos-esc-hint", Static).update(self._t("tui.repos_pick_first"))
            return
        row_values = table.get_row_at(row_index)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        name = str(row_values[0])  # pyright: ignore[reportUnknownArgumentType, reportUnknownIndexType]
        try:
            await self._service.plugin_repo_remove(name)
        except KeyError as exc:
            self.query_one("#repos-esc-hint", Static).update(error_message(self._service, exc))
            return
        self._reload()
        self.run_worker(
            self._refresh_status(), exclusive=True, group="repo-status", exit_on_error=False
        )
        self.query_one("#repos-esc-hint", Static).update(self._t("plugin.repo_removed", name=name))
