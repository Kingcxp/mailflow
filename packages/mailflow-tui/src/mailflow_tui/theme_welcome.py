"""First-launch theme choice shown before the main TUI tabs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from mailflow.themes import DEFAULT_THEME, TERMINAL_THEME
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.dom import DOMNode
from textual.screen import Screen
from textual.widgets import Button, Static

_CHOICES: dict[str, str | None] = {
    "theme-dark": DEFAULT_THEME,
    "theme-light": "catppuccin-latte",
    "theme-auto": TERMINAL_THEME,
    "theme-current": None,
}


class ThemeWelcomeScreen(Screen[str | None]):
    BINDINGS: ClassVar[list[Any]] = [("escape", "keep_current", "")]

    DEFAULT_CSS = """
    ThemeWelcomeScreen {
        align: center middle;
        background: $background;
    }
    #theme-welcome {
        width: 96%;
        max-width: 88;
        height: auto;
        max-height: 100%;
        overflow-y: auto;
        border: round $border;
        background: $surface;
        padding: 1 2;
    }
    #theme-welcome-title {
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }
    #theme-welcome-description {
        color: $text-muted;
        margin-bottom: 1;
    }
    .theme-choice {
        height: auto;
        min-height: 3;
        margin-bottom: 0;
        border-bottom: solid $border-blurred;
    }
    .theme-choice-preview {
        width: 8;
        height: 3;
        content-align: center middle;
        margin-right: 1;
    }
    .theme-choice-copy {
        width: 1fr;
        height: auto;
        margin-right: 1;
    }
    .theme-choice-title {
        text-style: bold;
        color: $text;
    }
    .theme-choice-description {
        color: $text-muted;
    }
    .theme-choice Button {
        width: 16;
        height: 3;
        margin: 0;
    }
    #theme-current {
        width: 100%;
        margin-top: 0;
    }
    """

    def __init__(
        self, t: Callable[[str], str], *, on_preview: Callable[[str | None], None]
    ) -> None:
        super().__init__()
        self._t = t
        self._on_preview = on_preview

    def compose(self) -> ComposeResult:
        dark_preview = Text()
        dark_preview.append("● ", style="#f4dbd6")
        dark_preview.append("● ", style="#8aadf4")
        dark_preview.append("●", style="#a6da95")
        light_preview = Text()
        light_preview.append("● ", style="#d20f39")
        light_preview.append("● ", style="#1e66f5")
        light_preview.append("●", style="#40a02b")
        auto_preview = Text()
        auto_preview.append("◐ ", style="#8aadf4")
        auto_preview.append("◑ ", style="#eed49f")
        auto_preview.append("◒", style="#a6da95")

        with Vertical(id="theme-welcome"):
            yield Static(self._t("tui.theme_welcome_title"), id="theme-welcome-title")
            yield Static(self._t("tui.theme_welcome_description"), id="theme-welcome-description")
            with Horizontal(id="theme-dark-row", classes="theme-choice"):
                yield Static(dark_preview, classes="theme-choice-preview")
                with Vertical(classes="theme-choice-copy"):
                    yield Static(self._t("tui.theme_welcome_dark"), classes="theme-choice-title")
                    yield Static(
                        self._t("tui.theme_welcome_dark_description"),
                        classes="theme-choice-description",
                    )
                yield Button(self._t("tui.theme_welcome_dark"), id="theme-dark", variant="primary")
            with Horizontal(id="theme-light-row", classes="theme-choice"):
                yield Static(light_preview, classes="theme-choice-preview")
                with Vertical(classes="theme-choice-copy"):
                    yield Static(self._t("tui.theme_welcome_light"), classes="theme-choice-title")
                    yield Static(
                        self._t("tui.theme_welcome_light_description"),
                        classes="theme-choice-description",
                    )
                yield Button(self._t("tui.theme_welcome_light"), id="theme-light")
            with Horizontal(id="theme-auto-row", classes="theme-choice"):
                yield Static(auto_preview, classes="theme-choice-preview")
                with Vertical(classes="theme-choice-copy"):
                    yield Static(self._t("tui.theme_welcome_auto"), classes="theme-choice-title")
                    yield Static(
                        self._t("tui.theme_welcome_auto_description"),
                        classes="theme-choice-description",
                    )
                yield Button(self._t("tui.theme_welcome_auto"), id="theme-auto")
            yield Button(self._t("tui.theme_welcome_current"), id="theme-current")

    def on_mount(self) -> None:
        self.query_one("#theme-dark", Button).focus()

    def _preview_node(self, node: DOMNode | None) -> None:
        while node is not None and node is not self:
            choice = (node.id or "").removesuffix("-row")
            if choice in _CHOICES:
                self._on_preview(_CHOICES[choice])
                return
            node = node.parent
        self._on_preview(None)

    def on_enter(self, event: events.Enter) -> None:
        self._preview_node(event.node)

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        self._preview_node(event.widget)

    def on_leave(self, event: events.Leave) -> None:
        if event.node is self:
            self._on_preview(None)

    def on_unmount(self) -> None:
        self._on_preview(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        choice = event.button.id or ""
        if choice in _CHOICES:
            event.stop()
            self.dismiss(_CHOICES[choice])

    def action_keep_current(self) -> None:
        self.dismiss(None)
