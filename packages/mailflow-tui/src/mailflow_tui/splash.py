"""A short, theme-aware mail-flow animation, with immediate keyboard/mouse skip.

The service is already running. This is a welcome, not a loading indicator.
Only the small artwork is repainted; elapsed time keeps slow terminals from
building up animation frames. No tasks or network requests belong here.
"""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from typing import ClassVar, cast

from mailflow.branding import PRODUCT_NAME
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Static

from mailflow_tui.animation import theme_palette, welcome_frame


class SplashScreen(ModalScreen[None]):
    """A skippable welcome which never pops a later screen or leaks timers."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,enter,space", "skip", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    SplashScreen {
        align: center middle;
        background: $ctp-base;
        overflow: hidden;
    }
    #splash-content {
        width: 100%;
        max-width: 76;
        height: auto;
    }
    #splash-content > Static {
        width: 100%;
        height: auto;
        content-align: center middle;
    }
    #splash-title {
        color: $ctp-text;
        text-style: bold;
        margin-top: 1;
    }
    #splash-tagline {
        color: $ctp-subtext0;
        margin-top: 1;
        margin-bottom: 1;
    }
    #splash-version {
        color: $ctp-overlay1;
    }
    #splash-skip {
        color: $ctp-subtext1;
        margin-top: 1;
    }
    SplashScreen.compact #splash-tagline {
        margin-bottom: 0;
    }
    """

    def __init__(
        self,
        t: Callable[[str], str],
        version: str = "",
        *,
        duration: float = 3.0,
    ) -> None:
        super().__init__()
        self._t = t
        self._version = version
        self._duration = duration
        self._started = 0.0
        self._motion_enabled = False
        self._finished = False
        self._art_size: tuple[int, bool] | None = None
        self._animation_timer: Timer | None = None
        self._dismiss_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="splash-content"):
            yield Static("", id="splash-art")
            yield Static(PRODUCT_NAME, id="splash-title")
            yield Static(self._t("tui.splash_tagline"), id="splash-tagline")
            yield Static(self._version, id="splash-version", markup=False)
            yield Static(self._t("tui.splash_skip"), id="splash-skip")

    def on_mount(self) -> None:
        app = cast(App[object], self.app)  # pyright: ignore[reportUnknownMemberType]
        self._started = monotonic()
        self._motion_enabled = app.animation_level != "none"
        self._render_art()
        if self._motion_enabled:
            # One small region at 16 fps; reduced motion uses 8 fps.
            interval = 1 / 8 if app.animation_level == "basic" else 1 / 16
            self._animation_timer = self.set_interval(interval, self._render_art)
        delay = self._duration if self._motion_enabled else min(self._duration, 0.5)
        self._dismiss_timer = self.set_timer(delay, self._finish)

    def on_resize(self, _event: events.Resize) -> None:
        if self.is_mounted and not self._finished:
            self._render_art()

    def _render_art(self) -> None:
        if self._finished:
            return
        palette = theme_palette(self)
        width = max(9, min(64, self.size.width - 4))
        compact = self.size.height < 22
        self.set_class(compact, "compact")
        elapsed = monotonic() - self._started if self._motion_enabled else self._duration
        art = welcome_frame(elapsed, width, palette, duration=self._duration, compact=compact)
        shape = (width, compact)
        self.query_one("#splash-art", Static).update(art, layout=shape != self._art_size)
        self._art_size = shape

    def _stop_timers(self) -> None:
        if self._animation_timer is not None:
            self._animation_timer.stop()
            self._animation_timer = None
        if self._dismiss_timer is not None:
            self._dismiss_timer.stop()
            self._dismiss_timer = None

    def _finish(self) -> None:
        if self._finished:
            return
        self._stop_timers()
        self._finished = True
        if self.is_current:
            self.dismiss()

    def on_unmount(self) -> None:
        self._finished = True
        self._stop_timers()

    def check_consume_key(self, key: str, character: str | None) -> bool:
        # Even global shortcuts skip first instead of opening a hidden dialog.
        return True

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        self._finish()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        event.stop()
        event.prevent_default()
        self._finish()

    def on_paste(self, event: events.Paste) -> None:
        event.stop()
        event.prevent_default()
        self._finish()

    def action_skip(self) -> None:
        self._finish()
