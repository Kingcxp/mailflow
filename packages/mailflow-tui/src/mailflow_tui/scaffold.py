"""Plugin scaffolding wizard: pick a folder, optionally create a subfolder,
choose a template category and generate a complete plugin template."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

from mailflow.plugin_template import CATEGORIES, scaffold_plugin
from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DirectoryTree, Input, Select, Static


class PluginScaffoldScreen(ModalScreen[Path | None]):
    """Directory-tree wizard that scaffolds a plugin into the chosen folder."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("escape", "dismiss_modal", "cancel")
    ]

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        with Vertical(id="scaffold-dialog"):
            yield Static(self._t("tui.scaffold_title"), classes="scaffold-title")
            yield Static(self._t("tui.scaffold_pick_folder"), classes="scaffold-hint")
            yield DirectoryTree(Path.cwd(), id="scaffold-tree")
            yield Checkbox(self._t("tui.scaffold_subfolder"), id="scaffold-subfolder")
            yield Input(
                placeholder=self._t("tui.scaffold_folder_name"),
                id="scaffold-folder-name",
                disabled=True,
            )
            yield Input(
                placeholder=self._t("tui.scaffold_plugin_id"),
                id="scaffold-plugin-id",
                value="mailflow-",
            )
            yield Select(
                [(f"{c} — {self._t(f'plugin.template.{c}')}", c) for c in CATEGORIES],
                id="scaffold-type",
                value=CATEGORIES[3],
            )
            with Horizontal(id="scaffold-actions"):
                yield Button(self._t("tui.btn_generate"), id="scaffold-generate", variant="success")
                yield Button(self._t("tui.btn_cancel"), id="scaffold-cancel", variant="primary")

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def on_mount(self) -> None:
        self.query_one("#scaffold-tree", DirectoryTree).focus()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "scaffold-subfolder":
            folder_input = self.query_one("#scaffold-folder-name", Input)
            folder_input.disabled = not event.value
            if event.value:
                folder_input.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "scaffold-cancel":
            self.dismiss(None)
        elif button_id == "scaffold-generate":
            self._generate()

    def _generate(self) -> None:
        tree = self.query_one("#scaffold-tree", DirectoryTree)
        node = tree.cursor_node
        if node is None or node.data is None:
            self._notify_error(self._t("tui.scaffold_pick_folder"))
            return
        base = Path(node.data.path)
        if base.is_file():
            base = base.parent
        plugin_id = self.query_one("#scaffold-plugin-id", Input).value.strip()
        try:
            type_select: Select[Any] = self.query_one("#scaffold-type", Select)  # pyright: ignore[reportUnknownVariableType]
            category = str(type_select.value)
            if self.query_one("#scaffold-subfolder", Checkbox).value:
                folder_name = self.query_one("#scaffold-folder-name", Input).value.strip()
                if not folder_name:
                    self._notify_error(self._t("tui.scaffold_folder_name"))
                    return
                target = base / folder_name
            else:
                target = base
            self.query_one("#scaffold-generate", Button).disabled = True
            self.run_worker(self._scaffold(target, plugin_id, category), exit_on_error=False)
        except ValueError as exc:
            self._notify_error(str(exc))

    async def _scaffold(self, target: Path, plugin_id: str, category: str) -> None:
        try:
            created = await asyncio.to_thread(scaffold_plugin, target, plugin_id, category)
        except (ValueError, OSError) as exc:
            self._notify_error(str(exc))
            generate_btn = self.query_one_optional("#scaffold-generate", Button)
            if generate_btn is not None:
                generate_btn.disabled = False
            return
        self.notify(self._t("tui.scaffold_created", path=str(created)), severity="information")
        self.dismiss(created)

    def _notify_error(self, message: str) -> None:
        self.notify(message, severity="error", timeout=6)
