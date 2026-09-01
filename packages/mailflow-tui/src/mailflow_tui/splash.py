"""Animated boot splash for the MailFlow TUI.

Shown for a moment while the app mounts (service is already started by the
runner; the splash just makes the handoff feel alive). Renders the logo with
a flowing brand-color wave, a tiny equalizer bar and a step-advancing status
line; dismisses itself after ``duration`` seconds or on Escape. Needs only
the translation callable — no service dependency.

Rendering is deliberately gentle: slow terminals (headless containers, web
shells) stall when a TUI floods them with frame updates, so the animation
ticks at ~5fps, is disabled entirely when the app's animation level is
``none``, and never uses Textual's `LoadingIndicator` (which repaints at
16fps on its own timer).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any, ClassVar

from rich.text import Text as RichText
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static

# MailFlow brand palette: the four urgency contract colors plus the accent.
# Drawn left-to-right as a moving wave; the logo cycles through them in
# lockstep with the equalizer so the whole screen breathes together.
_PALETTE: tuple[tuple[int, int, int], ...] = (
    (0x90, 0x93, 0x99),  # AD / gray
    (0x67, 0xC2, 0x3A),  # INFO / green
    (0xE6, 0xA2, 0x3C),  # IMPORTANT / amber
    (0xF5, 0x6C, 0x6C),  # URGENT / red
    (0x7E, 0xA7, 0xF8),  # accent blue
)

_LOGO = "MailFlow"

_LEVELS = "▁▃▅▇█"

# frames per second for the color wave — a visible-but-light animation;
# slow terminals still cope because each tick only rewrites two Statics
_TICK_INTERVAL = 1 / 8


def _color(palette_index: int) -> str:
    r, g, b = _PALETTE[palette_index % len(_PALETTE)]
    return f"#{r:02x}{g:02x}{b:02x}"


def _logo_text(frame: int) -> RichText:
    """The logo, each character tinted from a moving slice of the palette."""
    text = RichText(no_wrap=True)
    for index, char in enumerate(_LOGO):
        color = _color(int(frame / 2) + index * 2)
        text.append(char, style=f"bold {color}")
    return text


def _wave_text(frame: int) -> RichText:
    """A 20-bar equalizer; bar heights follow sine, tinted like the logo."""
    text = RichText(no_wrap=True)
    for bar in range(20):
        phase = bar * 0.55 + frame * 0.2
        height = int(1 + 4 * (0.5 + 0.5 * math.sin(phase)))
        color = _color(int(frame / 2) + bar * 2)
        text.append(_LEVELS[height - 1] * 2, style=color)
    return text


class SplashScreen(Screen[None]):
    """Full-screen boot animation; dismisses itself and returns to the app."""

    BINDINGS: ClassVar[list[Any]] = [Binding("escape", "skip", "Skip")]

    DEFAULT_CSS = """
    SplashScreen {
        align: center middle;
    }
    SplashScreen > Static {
        width: 100%;
        content-align: center middle;
    }
    #splash-logo {
        text-style: bold;
        margin-bottom: 1;
    }
    #splash-tagline {
        color: $text-muted;
        margin-bottom: 1;
    }
    #splash-wave {
        margin-bottom: 1;
    }
    #splash-status {
        color: $text-muted;
        margin-bottom: 1;
    }
    #splash-version {
        color: $text-disabled;
    }
    """

    _STATUS_STEPS = (
        "tui.splash_status_plugins",
        "tui.splash_status_service",
        "tui.splash_status_ready",
    )

    def __init__(
        self,
        t: Callable[[str], str],
        version: str = "",
        *,
        duration: float = 3.5,
    ) -> None:
        super().__init__()
        self._t = t
        self._version = version
        self._duration = duration
        self._frame = 0
        self._status_step = 0
        self._status_ticks = 0
        self._timer: Any = None

    def compose(self) -> ComposeResult:
        yield Static("", id="splash-logo")
        yield Static(self._t("tui.splash_tagline"), id="splash-tagline")
        yield Static("", id="splash-wave")
        yield Static("", id="splash-status")
        yield Static(self._version, id="splash-version")

    async def on_mount(self) -> None:
        self._render_logo()
        # honour the app's animation setting: headless/slow hosts (which set
        # animation_level to "none") get a static frame instead of a busy
        # timer that can stall a slow terminal's output pipe
        if self.app.animation_level != "none":  # pyright: ignore[reportUnknownMemberType]
            self._timer = self.set_interval(_TICK_INTERVAL, self._tick)
        else:
            status = self.query_one("#splash-status", Static)
            status.update(self._t(self._STATUS_STEPS[-1]))  # pyright: ignore[reportUnknownMemberType]
        self.set_timer(self._duration, self._finish)

    def _finish(self) -> None:
        """Return to the main screen, stopping the animation first.

        ``pop_screen`` returns an AwaitComplete that must not be awaited from
        a message handler (Textual forbids it); fire-and-forget matches how
        the app itself pushes screens. Stopping the tick timer explicitly
        guarantees the animation can never outlive the screen even if the
        pop is deferred."""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]

    def _render_logo(self) -> None:
        logo = self.query_one("#splash-logo", Static)
        logo.update(_logo_text(self._frame))  # pyright: ignore[reportUnknownMemberType]
        wave = self.query_one("#splash-wave", Static)
        wave.update(_wave_text(self._frame))  # pyright: ignore[reportUnknownMemberType]

    def _tick(self) -> None:
        self._frame += 1
        self._render_logo()
        self._status_ticks += 1
        if self._status_ticks % 2 == 0 and self._status_step < len(self._STATUS_STEPS) - 1:
            self._status_step += 1
            status = self.query_one("#splash-status", Static)
            status.update(self._t(self._STATUS_STEPS[self._status_step]))  # pyright: ignore[reportUnknownMemberType]

    def action_skip(self) -> None:
        """Escape closes the splash early."""
        self._finish()
