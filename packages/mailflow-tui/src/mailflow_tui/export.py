"""Bot-framework export wizard: pick a framework and a folder in the
directory tree (optionally creating a subfolder), then generate the chatbot
framework plugin package via the registered exporter plugins."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from mailflow import __version__
from mailflow.bot_export import export_bot_plugin
from mailflow.domain import ComponentKind
from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DirectoryTree, Input, Select, Static


class BotExportScreen(ModalScreen[Path | None]):
    """Directory-tree wizard that exports MailFlow as a chatbot framework plugin."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("escape", "dismiss_modal", "cancel")
    ]

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        frameworks = self._service.registry.component_ids(ComponentKind.BOT_EXPORTER)
        with Vertical(id="export-dialog"):
            yield Static(self._t("tui.export_title"), classes="scaffold-title")
            yield Select(
                [(fw, fw) for fw in frameworks],
                id="export-framework",
                value=frameworks[0] if frameworks else None,
            )
            yield Static(self._t("tui.export_pick_folder"), classes="scaffold-hint")
            yield DirectoryTree(Path.cwd(), id="export-tree")
            yield Checkbox(self._t("tui.scaffold_subfolder"), id="export-subfolder")
            yield Input(
                placeholder=self._t("tui.scaffold_folder_name"),
                id="export-folder-name",
                disabled=True,
            )
            with Horizontal(id="export-actions"):
                yield Button(self._t("tui.btn_generate"), id="export-run", variant="success")
                yield Button(self._t("tui.btn_cancel"), id="export-cancel", variant="primary")

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def on_mount(self) -> None:
        self.query_one("#export-tree", DirectoryTree).focus()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "export-subfolder":
            folder_input = self.query_one("#export-folder-name", Input)
            folder_input.disabled = not event.value
            if event.value:
                folder_input.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "export-cancel":
            self.dismiss(None)
        elif button_id == "export-run":
            self._generate()

    def _generate(self) -> None:
        tree = self.query_one("#export-tree", DirectoryTree)
        node = tree.cursor_node
        if node is None or node.data is None:
            self._notify_error(self._t("tui.scaffold_pick_folder"))
            return
        base = Path(node.data.path)
        if base.is_file():
            base = base.parent
        framework_select: Select[Any] = self.query_one("#export-framework", Select)  # pyright: ignore[reportUnknownVariableType]
        framework = str(framework_select.value)
        if not framework:
            self._notify_error(self._t("tui.export_no_framework"))
            return
        target = base
        if self.query_one("#export-subfolder", Checkbox).value:
            folder_name = self.query_one("#export-folder-name", Input).value.strip()
            if not folder_name:
                self._notify_error(self._t("tui.scaffold_folder_name"))
                return
            target = base / folder_name
        self.run_worker(self._export(target, framework))

    async def _export(self, target: Path, framework: str) -> None:
        try:
            service = self._service
            # the generated plugin depends on runtime components, not on exporters
            plugin_ids = [
                info.plugin_id
                for info in service.plugin_manager.enabled_infos()
                if ComponentKind.BOT_EXPORTER not in info.kinds
            ]
            result = export_bot_plugin(
                service.registry,
                service.config,
                framework=framework,
                output_dir=target,
                plugin_ids=plugin_ids,
                version=__version__,
                language=service.i18n.language,
            )
        except Exception as exc:
            self.notify(self._t("tui.export_failed", message=str(exc)), severity="error", timeout=6)
            return
        self.notify(
            self._t("tui.export_created", count=len(result.created), path=str(target)),
            timeout=5,
        )
        self.dismiss(target)

    def _notify_error(self, message: str) -> None:
        self.notify(message, severity="error", timeout=6)
