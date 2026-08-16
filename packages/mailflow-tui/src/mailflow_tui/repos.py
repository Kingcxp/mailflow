"""Remote repository manager: list, add and remove plugin marketplace
connections from the TUI (the same surface as `plugin repo add|remove`)."""

from __future__ import annotations

from typing import Any, ClassVar

from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static


class ReposScreen(ModalScreen[bool | None]):
    """Manage remote plugin repositories (add / remove connections)."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("escape", "dismiss_modal", "cancel")
    ]

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service

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
                yield Button(self._t("tui.btn_cancel"), id="repos-cancel")

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def on_mount(self) -> None:
        table = self.query_one("#repos-table", DataTable)  # pyright: ignore[reportUnknownVariableType]
        table.add_column(self._t("plugin.header_name"), key="name")  # pyright: ignore[reportUnknownMemberType]
        table.add_column(self._t("tui.repos_url_header"), key="url")  # pyright: ignore[reportUnknownMemberType]
        self._reload()

    def _reload(self) -> None:
        table = self.query_one("#repos-table", DataTable)  # pyright: ignore[reportUnknownVariableType]
        table.clear()  # pyright: ignore[reportUnknownMemberType]
        for repo in self._service.config.plugins.repositories:
            table.add_row(repo.name, repo.url, key=repo.name)  # pyright: ignore[reportUnknownMemberType]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "repos-cancel":
            self.dismiss(None)
            return
        if button_id == "repos-add":
            self.run_worker(self._add_repo())
            return
        if button_id == "repos-remove":
            self.run_worker(self._remove_repo())

    async def _add_repo(self) -> None:
        name = self.query_one("#repos-name", Input).value.strip()
        url = self.query_one("#repos-url", Input).value.strip()
        if not name or not url:
            self.notify(self._t("tui.repos_missing_fields"), severity="error", timeout=6)
            return
        try:
            await self._service.plugin_repo_add(name, url)
        except ValueError as exc:
            self.notify(str(exc), severity="error", timeout=6)
            return
        self.query_one("#repos-name", Input).value = ""
        self.query_one("#repos-url", Input).value = ""
        self._reload()
        self.notify(self._t("plugin.repo_added", name=name), timeout=5)

    async def _remove_repo(self) -> None:
        table = self.query_one("#repos-table", DataTable)  # pyright: ignore[reportUnknownVariableType]
        row_key = table.cursor_row
        if row_key is None:  # pyright: ignore[reportUnnecessaryComparison]
            self.notify(self._t("tui.repos_pick_first"), severity="error", timeout=6)
            return
        row = table.get_row_at(row_key)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        name = str(row[0])  # pyright: ignore[reportUnknownArgumentType, reportUnknownIndexType]
        try:
            await self._service.plugin_repo_remove(name)
        except KeyError as exc:
            self.notify(str(exc), severity="error", timeout=6)
            return
        self._reload()
        self.notify(self._t("plugin.repo_removed", name=name), timeout=5)
