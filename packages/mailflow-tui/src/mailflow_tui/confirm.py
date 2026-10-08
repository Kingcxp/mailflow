"""Modal confirmation for the Mail tab's bulk actions.

Bulk operations (clearing expired mail, re-analyzing everything) are either
destructive or expensive, so they never run straight from a toolbar click: the
dialog states what will happen, with the real count, and the user confirms.
Cancel is a first-class button, not only Escape.

The dialog is deliberately reachable at any terminal size: a long list of
staged changes scrolls inside a bounded body instead of growing the box until
the buttons fall off the screen.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Button, Static

_ConfirmVariant = Literal["default", "primary", "success", "warning", "error"]


class ConfirmModal(ModalScreen[bool]):
    """Ask before a bulk action; returns True only on the confirm button."""

    BINDINGS: ClassVar[list[Any]] = []

    def __init__(
        self,
        service: MailFlowService,
        *,
        title: str,
        body: str,
        confirm_label: str,
        variant: _ConfirmVariant = "warning",
        focus_confirm: bool = True,
    ) -> None:
        super().__init__()
        self._service = service
        self._title = title
        self._body = body
        self._confirm_label = confirm_label
        self._variant: _ConfirmVariant = variant
        # A destructive action (delete, purge) starts on Cancel so a stray
        # Enter cannot trigger it; a plan the user explicitly asked for — the
        # staged changes of a smart action — starts on the confirm button,
        # because that Enter *is* how they accept it.
        self._focus_confirm = focus_confirm
        self._bindings.bind("escape", "cancel", self._service.t("tui.btn_cancel"))

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(escape(self._title), id="confirm-title")
            # a bounded, scrollable body: 12 staged changes on a 24-row
            # terminal used to push the buttons past the bottom of the screen,
            # leaving no visible way to confirm
            with ScrollableContainer(id="confirm-body-scroll"):
                yield Static(escape(self._body), id="confirm-body")
            with Horizontal(id="confirm-buttons"):
                yield Button(self._confirm_label, id="confirm-run", variant=self._variant)
                yield Button(self._service.t("tui.btn_cancel"), id="confirm-cancel")

    def on_mount(self) -> None:
        """Focus the button this dialog's default should land on."""
        target = "#confirm-run" if self._focus_confirm else "#confirm-cancel"
        button = self.query_one_optional(target, Button)
        if button is not None:
            button.focus()

    def _dismiss_once(self, result: bool) -> None:
        """Dismiss at most once: a double click or Escape must not pop the
        whole screen stack (Textual raises when the stack would go empty)."""
        if self.is_current:
            self.dismiss(result)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "confirm-run":
            self._dismiss_once(True)
        elif event.button.id == "confirm-cancel":
            self._dismiss_once(False)

    def action_cancel(self) -> None:
        self._dismiss_once(False)
