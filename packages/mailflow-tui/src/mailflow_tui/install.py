"""Local plugin installer: pick a folder in the directory tree — either a
single plugin folder or a folder holding several independent plugin folders
— and install every plugin found in it."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from mailflow.plugin_market import MarketPlugin, detect_plugin_folders
from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Static


class InstallScreen(ModalScreen[list[str] | None]):
    """Directory-tree wizard that installs local plugins (single or batch)."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("escape", "dismiss_modal", "cancel")
    ]

    def __init__(self, service: MailFlowService) -> None:
        super().__init__()
        self._service = service

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        with Vertical(id="install-dialog"):
            yield Static(self._t("tui.install_title"), classes="scaffold-title")
            yield Static(self._t("tui.install_pick_folder"), classes="scaffold-hint")
            yield DirectoryTree(Path.cwd(), id="install-tree")
            with Horizontal(id="install-actions"):
                yield Button(self._t("tui.btn_install"), id="install-run", variant="success")
                yield Button(self._t("tui.btn_cancel"), id="install-cancel")

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
            folders = detect_plugin_folders(base)
            if not folders:
                self.notify(
                    self._t("plugin.local_none_found", path=str(base)),
                    severity="error",
                    timeout=6,
                )
                return
            installed: list[str] = []
            failed: list[str] = []
            for folder in folders:
                plugin_id = self._plugin_id_of(folder)
                try:
                    await self._service.market.install(
                        MarketPlugin(
                            id=plugin_id,
                            name=folder.name,
                            version="",
                            categories=[],
                            package=plugin_id,
                            source=str(folder),
                        )
                    )
                except Exception as exc:
                    failed.append(f"{plugin_id}: {exc}")
                    continue
                await self._service.record_plugin_source(plugin_id, str(folder))
                installed.append(plugin_id)
            if installed:
                self.notify(
                    self._t(
                        "plugin.local_installed", count=len(installed), plugins=", ".join(installed)
                    ),
                    timeout=6,
                )
            if failed:
                self.notify(
                    self._t("plugin.local_failed", detail="; ".join(failed)),
                    severity="error",
                    timeout=8,
                )
            self.dismiss(installed or None)
        except Exception as exc:
            self.notify(str(exc), severity="error", timeout=8)
            self.dismiss(None)

    @staticmethod
    def _plugin_id_of(folder: Path) -> str:
        import json as jsonlib

        metadata_path = folder / "plugin.json"
        if metadata_path.is_file():
            try:
                payload = jsonlib.loads(metadata_path.read_text(encoding="utf-8"))
                plugin_id = payload.get("id")
                if isinstance(plugin_id, str) and plugin_id:
                    return plugin_id
            except (jsonlib.JSONDecodeError, OSError):
                pass
        return folder.name
