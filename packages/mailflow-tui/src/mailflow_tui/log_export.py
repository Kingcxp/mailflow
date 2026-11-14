"""Export the Logs tab buffer to a file.

A modal over the Logs pane: pick a target directory with a DirectoryTree,
type a file name (a ``.log`` suffix is appended when absent), and save
the selected filtered content, or the complete buffered content when chosen. The saved path is written back into the log buffer so
the user sees the confirmation next to the logs they exported.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar, cast

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Select, Static

from mailflow_tui.labels import error_detail
from mailflow_tui.widgets import ButtonRow, Input, SafeSelect


class LogExportScreen(ModalScreen[str | None]):
    """Pick a directory + file name and save the log buffer to it.

    Dismisses with the absolute path written, or ``None`` when cancelled."""

    # buffered lines are injected by the LogsPane (list[str] in queue format)

    BINDINGS: ClassVar[list[Any]] = []

    def __init__(
        self, service: Any, lines: list[str], *, filtered: list[str] | None = None
    ) -> None:
        super().__init__()
        self._service = service
        self._lines = lines
        self._filtered = lines if filtered is None else filtered
        self._saving = False
        self._bindings.bind("escape", "cancel", self._t("tui.btn_cancel"))

    def _t(self, key: str, **params: Any) -> str:
        return str(self._service.t(key, **params))

    def compose(self) -> ComposeResult:
        with Vertical(id="log-export-dialog"):
            yield Static(self._t("tui.log_export_title"), id="log-export-title")
            yield SafeSelect(
                [
                    (self._t("interface.current_filter"), "filtered"),
                    (self._t("interface.all_buffer"), "all"),
                ],
                value="filtered",
                allow_blank=False,
                id="log-export-scope",
            )
            yield Static(self._t("tui.log_export_pick_folder"), classes="scaffold-hint")
            yield DirectoryTree(Path.cwd(), id="log-export-tree")
            yield Input(
                placeholder=self._t("tui.log_export_filename_placeholder"),
                id="log-export-filename",
            )
            yield Static("", id="log-export-error")
            with ButtonRow(id="log-export-actions"):
                yield Button(self._t("tui.btn_save"), id="log-export-save", variant="primary")
                yield Button(self._t("tui.btn_cancel"), id="log-export-cancel", variant="default")

    async def on_mount(self) -> None:
        self.query_one("#log-export-tree", DirectoryTree).focus()  # pyright: ignore[reportUnknownMemberType]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "log-export-save":
            if not self._saving:
                self._saving = True
                self.query_one("#log-export-save", Button).disabled = True
                self.run_worker(self._save(), group="log-export", exit_on_error=False)
        elif event.button.id == "log-export-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        if not self._saving:
            self.dismiss(None)

    async def _save(self) -> None:
        try:
            await self._write_log()
        finally:
            self._saving = False
            self.query_one("#log-export-save", Button).disabled = False

    async def _write_log(self) -> None:
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
        error.update(self._t("tui.form_saving"))
        lines = (
            self._lines
            if cast("Select[str]", self.query_one("#log-export-scope", Select)).value == "all"
            else self._filtered
        )
        try:
            await asyncio.to_thread(
                target.write_text,
                "\n".join(lines) + ("\n" if lines else ""),
                encoding="utf-8",
            )
        except OSError as exc:
            detail = escape(error_detail(self._service, exc))
            error.update(f"[red]{self._t('tui.log_export_failed', error=detail)}[/red]")
            return
        self.dismiss(str(target))
