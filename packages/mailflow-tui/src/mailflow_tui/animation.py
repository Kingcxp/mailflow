"""Small ASCII mailroom scenes shared by the welcome and smart operations."""

from __future__ import annotations

import math
from time import monotonic
from typing import Literal, cast

from rich.text import Text
from textual import events
from textual.app import App
from textual.color import Color
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

Scene = Literal["welcome", "thinking", "reading", "applying", "review", "stopping"]
_ACTIVE_SCENES = {"welcome", "thinking", "reading", "applying"}


def theme_palette(widget: Widget) -> dict[str, Color]:
    app = cast(App[object], widget.app)  # pyright: ignore[reportUnknownMemberType]
    variables = app.current_theme.variables
    palette = {
        name: Color.parse(variables[f"ctp-{name}"])
        for name in ("surface1", "blue", "mauve", "teal", "text", "green", "yellow")
    }
    palette["primary"] = Color.parse(
        variables.get("button-primary-background", variables["ctp-blue"])
    )
    return palette


def welcome_frame(
    elapsed: float,
    width: int,
    palette: dict[str, Color],
    *,
    duration: float = 3.0,
    compact: bool = False,
) -> Text:
    """One letter folds into a paper bird and lands in an inbox, without looping."""
    width = max(9, min(width, 64))
    small = compact or width < 40
    height = 7 if small else 11
    grid = [[(" ", "") for _ in range(width)] for _ in range(height)]
    phase = min(1.0, max(0.0, elapsed / max(0.1, duration)))
    quiet = palette["surface1"].hex
    blue = (
        palette["surface1"]
        .blend(palette.get("primary", palette["blue"]), min(1.0, 0.65 + phase * 1.75))
        .hex
    )
    crease = palette["teal"].hex

    def draw(
        x: float, y: float, rows: tuple[str, ...], color: str, *, opaque: bool = False
    ) -> None:
        left = round(x - max(map(len, rows)) / 2)
        top = round(y - (len(rows) - 1) / 2)
        for row, line in enumerate(rows, top):
            for offset, char in enumerate(line):
                column = left + offset
                if (char != " " or opaque) and 0 <= column < width and 0 <= row < height:
                    grid[row][column] = (char, color)

    start_x = min(13.0, width * 0.27)
    finish_x = float(width - (6 if small else 11))
    if width < 24:
        start_x = finish_x = width / 2
    start_y = (height - 1) / 2
    flight = min(1.0, max(0.0, (phase - 0.4) / 0.48))
    ease = flight * flight * (3 - 2 * flight)
    x = start_x + (finish_x - start_x) * ease
    y = start_y - math.sin(flight * math.pi) * (0.5 if small else 1.5)
    if phase < 0.2:
        x -= (3 if small else 5) * (1 - phase / 0.2)
    tray = ("\\       /", " '-----' ")
    if width >= 24:
        draw(finish_x, height - 1.5, tray, crease if phase >= 0.88 else quiet)
    if 0.4 < phase < 0.94:
        for step in range(1, 10):
            trail = max(0.0, flight - step * 0.04)
            trail_ease = trail * trail * (3 - 2 * trail)
            if trail > 0:
                draw(
                    start_x + (finish_x - start_x) * trail_ease,
                    start_y - math.sin(trail * math.pi) * (0.5 if small else 1.5),
                    (".",),
                    quiet,
                )
    if phase >= 0.88:
        landing = min(1.0, (phase - 0.88) / 0.12)
        y += (height - 3 - y) * landing
        draw(x, y, (".---.", "|\\ /|", "'---'"), blue)
    elif phase < 0.2:
        rows: tuple[str, ...] = (
            ".--------------.",
            "|\\            /|",
            "| '.        .' |",
            "|   '------'   |",
            "'--------------'",
        )
        if small:
            rows = (".-------.", "|\\     /|", "| '---' |", "'-------'")
        draw(x, y, rows, blue, opaque=True)
    elif phase < 0.32:
        rows = (
            "      /\\",
            "     /  \\__",
            "    /  /   /|",
            "   /__/___/ |",
            "   \\  \\   \\ /",
            "    \\__\\___/",
        )
        if small:
            rows = ("  /\\", " /__\\", " \\  /", "  \\/")
        draw(x, y, rows, crease)
    else:
        poses = (
            (
                "        /|",
                "       / |",
                "      /  |    __",
                "     /   |___/ _>",
                "    /         /",
                "    \\________/",
                "        \\  /",
                "         \\/",
            ),
            (
                "",
                "",
                "  _______     __",
                "  \\      \\___/ _>",
                "   \\          /",
                "    \\________/",
                "        \\  /",
                "         \\/",
            ),
            (
                "",
                "",
                "              __",
                "    _________/ _>",
                "    \\          /",
                "     \\___   __/",
                "        /  /",
                "       /__/",
            ),
        )
        wing = (0, 1, 2, 1)[int(max(0.0, phase - 0.32) * 24) % 4]
        rows = (
            poses[wing]
            if not small
            else (("  /|", "_/ |__", "\\____>") if wing == 0 else ("_____/>", "\\___/", "  \\/"))
        )
        draw(x, y, rows, blue, opaque=True)

    text = Text(no_wrap=True, overflow="crop")
    for row, cells in enumerate(grid):
        if row:
            text.append("\n")
        for char, color in cells:
            text.append(char, style=color)
    return text


def mailroom_frame(
    elapsed: float, width: int, palette: dict[str, Color], scene: Scene, *, compact: bool = False
) -> Text:
    """Letters pass a blinking mailroom helper; this is activity, not progress."""
    width = max(9, min(width, 60))
    accent = {
        "welcome": "teal",
        "thinking": "mauve",
        "reading": "blue",
        "applying": "teal",
        "review": "yellow",
        "stopping": "surface1",
    }[scene]
    blink = int(elapsed * 3) % 11 == 10
    eyes = "- -" if blink else "o o"
    if scene == "review":
        eyes = "^ ^"
    elif scene == "stopping":
        eyes = "- -"
    elif scene == "reading":
        eyes = "o O" if int(elapsed * 2) % 2 else "O o"
    mouth = {"thinking": " ? ", "reading": " _ ", "applying": " = "}.get(scene, " v ")
    if compact:
        text = Text(f"[{eyes.replace(' ', '_')}]  ", style=palette[accent].hex, no_wrap=True)
        if scene in {"review", "stopping"}:
            trail = "   [?]  " if scene == "review" else "   [ ]  "
        else:
            offset = int(elapsed * 5) % 8
            trail = "." * offset + ">" + "." * (7 - offset)
        text.append(trail, style=palette[accent].hex)
        return text

    grid = [[(" ", "") for _ in range(width)] for _ in range(5)]
    center = (width - 9) // 2
    for x in range(width):
        if x < center or x >= center + 9:
            grid[2][x] = ("." if x % 3 == 0 else "-", palette["surface1"].hex)

    def draw(x: int, y: int, rows: tuple[str, ...], color: str) -> None:
        for row, line in enumerate(rows, y):
            for offset, char in enumerate(line):
                column = x + offset
                if 0 <= column < width:
                    grid[row][column] = (char, color)

    if width >= 30 and scene in _ACTIVE_SCENES:
        # Each letter disappears into the helper before the next is dispatched.
        for side, ink in ((0, "blue"), (1, "teal")):
            phase = (elapsed * 0.6 + side * 0.45) % 1
            travel = center + 5
            x = round(-5 + phase * travel) if side == 0 else round(center + 9 + phase * travel)
            y = 1 if side == 0 else round(1 - math.sin(phase * math.pi))
            color = palette["surface1"].blend(palette[ink], min(1.0, phase * 5)).hex
            letter = ("+---+", "|\\ /|", "+---+") if side == 0 else (".---.", "| = |", "'---'")
            draw(x, y, letter, color)
        scanner = round((math.sin(elapsed * 2.2) + 1) / 2 * max(0, center - 2))
        grid[2][scanner] = (">", palette["blue"].hex)

    beacon = "*" if int(elapsed * 2) % 2 else "o"
    draw(
        center,
        0,
        (f"   .{beacon}.   ", " .-' '-. ", f" | {eyes} | ", f" | {mouth} | ", " '-----' "),
        palette[accent].hex,
    )
    text = Text(no_wrap=True, overflow="crop")
    for row, line in enumerate(grid):
        if row:
            text.append("\n")
        for char, color in line:
            text.append(char, style=color)
    return text


class MailAnimation(Static):
    """A bounded repaint region which stops on hide, review or cancellation."""

    DEFAULT_CSS = """
    MailAnimation { height: 6; color: $ctp-subtext0; }
    MailAnimation.compact { height: 1; }
    """

    def __init__(self, *, id: str) -> None:
        super().__init__("", id=id, markup=False)
        self._scene: Scene = "thinking"
        self._caption = ""
        self._compact = True
        self._started = monotonic()
        self._timer: Timer | None = None

    def set_activity(self, scene: Scene, caption: str, *, compact: bool) -> None:
        self._scene, self._caption, self._compact = scene, caption, compact
        self.set_class(compact, "compact")
        if self.is_mounted:
            self._render_frame()
            self._sync_timer()

    def _sync_timer(self) -> None:
        app = cast(App[object], self.app)  # pyright: ignore[reportUnknownMemberType]
        running = self._scene in _ACTIVE_SCENES and self.display and app.animation_level != "none"
        if not running:
            self._stop_timer()
        elif self._timer is None:
            self._timer = self.set_interval(
                1 / 8 if app.animation_level == "basic" else 1 / 16, self._tick
            )

    def _tick(self) -> None:
        if self.is_on_screen and self.screen.is_current:
            self._render_frame()

    def _render_frame(self) -> None:
        app = cast(App[object], self.app)  # pyright: ignore[reportUnknownMemberType]
        elapsed = monotonic() - self._started if app.animation_level != "none" else 0.0
        width = max(9, self.size.width)
        text = mailroom_frame(
            elapsed, width, theme_palette(self), self._scene, compact=self._compact
        )
        text.append("  " if self._compact else "\n")
        text.append(self._caption, style="$ctp-subtext0")
        self.update(text, layout=False)

    def on_mount(self) -> None:
        self._render_frame()
        self._sync_timer()

    def on_show(self) -> None:
        self._sync_timer()

    def on_resize(self, _event: events.Resize) -> None:
        self._render_frame()

    def on_hide(self) -> None:
        self._stop_timer()

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def on_unmount(self) -> None:
        self._stop_timer()
