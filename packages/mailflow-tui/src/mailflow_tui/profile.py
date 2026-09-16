"""The recipient profile form.

The user describes who they are and which mail they care about; the text is
stored once and handed to the LLM with every analysis and smart action, so
classification is tuned to them instead of to a generic student. It is plain
user-authored context — the model is told to treat it as information about the
recipient, never as instructions it must follow for the mail itself.
"""

from __future__ import annotations

from typing import Any, ClassVar

from mailflow.service import MailFlowService
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

from mailflow_tui.labels import error_detail


class UserProfileModal(ModalScreen[bool]):
    """Edit and save the recipient profile (empty clears it)."""

    BINDINGS: ClassVar[list[Any]] = []

    def __init__(self, service: MailFlowService, profile: str) -> None:
        super().__init__()
        self._service = service
        self._bindings.bind("escape", "cancel", self._service.t("tui.btn_cancel"))
        self._bindings.bind("ctrl+s", "save", self._service.t("tui.btn_save"))
        self._initial = profile

    def _t(self, key: str, **params: Any) -> str:
        return self._service.t(key, **params)

    def compose(self) -> ComposeResult:
        with Vertical(id="profile-dialog"):
            yield Static(self._t("tui.profile_title"), id="profile-title")
            yield Static(escape(self._t("tui.profile_hint")), id="profile-hint")
            yield Static(self._t("tui.profile_label"), classes="todo-label")
            with ScrollableContainer(id="profile-scroll"):
                yield TextArea(self._initial, id="profile-text")
            yield Static("", id="profile-status")
            with Horizontal(id="profile-buttons"):
                yield Button(self._t("tui.btn_save"), id="profile-save", variant="success")
                yield Button(self._t("tui.btn_cancel"), id="profile-cancel", variant="default")

    async def on_mount(self) -> None:
        self.query_one("#profile-text", TextArea).focus()  # pyright: ignore[reportUnknownMemberType]

    def _dismiss_once(self, result: bool) -> None:
        """Dismiss at most once: Escape, ctrl+s and a click can race, and a
        second pop would take the whole screen stack down."""
        if self.is_current:
            self.dismiss(result)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "profile-save":
            await self._save()
        elif event.button.id == "profile-cancel":
            self._dismiss_once(False)

    async def _save(self) -> None:
        text = self.query_one("#profile-text", TextArea).text
        try:
            stored = await self._service.set_user_profile(text)
        except Exception as exc:
            self.query_one("#profile-status", Static).update(
                f"[red]{escape(self._t('tui.profile_save_failed', error=error_detail(self._service, exc)))}[/red]"
            )
            return
        status = self._t("tui.profile_saved") if stored else self._t("tui.profile_cleared")
        self.query_one("#profile-status", Static).update(f"[green]{escape(status)}[/green]")
        self._dismiss_once(True)

    def action_save(self) -> None:
        self.run_worker(self._save(), exclusive=True, group="profile-save", exit_on_error=False)

    def action_cancel(self) -> None:
        self._dismiss_once(False)
