"""Local plugin installer: pick a folder in the directory tree — either a
single plugin folder or a folder holding several independent plugin folders
— and install every plugin found in it."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Static

from mailflow_tui.labels import error_message


class InstallScreen(ModalScreen[list[str] | None]):
    """Directory-tree wizard that installs local plugins (single or batch)."""

    BINDINGS: ClassVar[list[Any]] = []

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service
        self._bindings.bind("escape", "dismiss_modal", self._t("tui.btn_cancel"))

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        with Vertical(id="install-dialog"):
            yield Static(self._t("tui.install_title"), classes="scaffold-title")
            yield Static(self._t("tui.install_pick_folder"), classes="scaffold-hint")
            yield DirectoryTree(Path.cwd(), id="install-tree")
            with Horizontal(id="install-actions"):
                yield Button(self._t("tui.btn_install"), id="install-run", variant="success")
                yield Button(self._t("tui.btn_cancel"), id="install-cancel", variant="primary")

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def on_mount(self) -> None:
        self.query_one("#install-tree", DirectoryTree).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "install-cancel":
            self.dismiss(None)
        elif button_id == "install-run":
            self._run_install()

    def _run_install(self) -> None:
        tree = self.query_one("#install-tree", DirectoryTree)
        node = tree.cursor_node
        if node is None or node.data is None:
            self.notify(self._t("tui.install_pick_folder"), severity="error", timeout=6)
            return
        base = Path(node.data.path)
        if base.is_file():
            base = base.parent
        self.run_worker(self._install(base))

    async def _install(self, base: Path) -> None:
        try:
            output = await self._service.plugin_install_local(base)
            self.notify(output, timeout=8)
            self.dismiss([])
        except Exception as exc:
            self.notify(error_message(self._service, exc), severity="error", timeout=8)
            self.dismiss(None)
