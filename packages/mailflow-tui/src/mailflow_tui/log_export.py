"""Export the Logs tab buffer to a file.

A modal over the Logs pane: pick a target directory with a DirectoryTree,
type a file name (a ``.log`` suffix is appended when absent), and save
the exact buffered content — the same lines the viewer renders, unfiltered
so nothing is lost. The saved path is written back into the log buffer so
the user sees the confirmation next to the logs they exported.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Input, Static


class LogExportScreen(ModalScreen[str | None]):
    """Pick a directory + file name and save the log buffer to it.

    Dismisses with the absolute path written, or ``None`` when cancelled."""

    # buffered lines are injected by the LogsPane (list[str] in queue format)

    BINDINGS: ClassVar[list[Any]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, service: Any, lines: list[str]) -> None:
        super().__init__()
        self._service = service
        self._lines = lines

    def _t(self, key: str, **params: Any) -> str:
        return str(self._service.t(key, **params))

    def compose(self) -> ComposeResult:
        with Vertical(id="log-export-dialog"):
            yield Static(self._t("tui.log_export_title"), id="log-export-title")
            yield Static(self._t("tui.log_export_pick_folder"), classes="scaffold-hint")
            yield DirectoryTree(Path.cwd(), id="log-export-tree")
            yield Input(
                placeholder=self._t("tui.log_export_filename_placeholder"),
                id="log-export-filename",
            )
            yield Static("", id="log-export-error")
            with Horizontal(id="log-export-actions"):
                yield Button(self._t("tui.btn_save"), id="log-export-save", variant="primary")
                yield Button(self._t("tui.btn_cancel"), id="log-export-cancel", variant="default")

    async def on_mount(self) -> None:
        self.query_one("#log-export-tree", DirectoryTree).focus()  # pyright: ignore[reportUnknownMemberType]

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "log-export-save":
            await self._save()
        elif event.button.id == "log-export-cancel":
            self.dismiss(None)

    async def _save(self) -> None:
        error = self.query_one("#log-export-error", Static)
        tree = self.query_one("#log-export-tree", DirectoryTree)
        node = tree.cursor_node
        if node is None or node.data is None:
            error.update(f"[yellow]{self._t('tui.scaffold_pick_folder')}[/yellow]")
            return
        base = Path(str(node.data.path))
        if base.is_file():  # noqa: ASYNC240 — a stat on a picked path is instant
            base = base.parent
        name = str(self.query_one("#log-export-filename", Input).value).strip()
        if not name:
            error.update(f"[yellow]{self._t('tui.log_export_filename_required')}[/yellow]")
            return
        if not name.endswith(".log"):
            name += ".log"
        target = base / name
        try:
            target.write_text(
                "\n".join(self._lines) + ("\n" if self._lines else ""), encoding="utf-8"
            )
        except OSError as exc:
            error.update(f"[red]{self._t('tui.log_export_failed', error=str(exc))}[/red]")
            return
        self.dismiss(str(target))
