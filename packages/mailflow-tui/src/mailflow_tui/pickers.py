"""Optional selection helpers; the underlying text fields stay editable."""

from __future__ import annotations

import asyncio
import calendar
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, cast
from zoneinfo import ZoneInfo

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.geometry import Offset, Size
from textual.screen import ModalScreen, Screen
from textual.signal import Signal
from textual.widget import Widget
from textual.widgets import Button, DataTable, DirectoryTree, OptionList, Select, Static
from textual.widgets.option_list import Option

from mailflow_tui.widgets import ContentDialog, Input, SafeSelect


class PopupMenu(OptionList):
    """A single-click action menu over the current page, without a new screen."""

    BINDINGS: ClassVar[list[Any]] = [Binding("escape", "cancel", show=False)]
    DEFAULT_CSS = """
    PopupMenu {
        overlay: screen; position: absolute; layer: overlay;
        width: auto; height: auto; max-width: 100%; max-height: 100%;
        constrain: inside;
        border: round $ctp-surface1; padding: 0;
        background: $ctp-mantle; color: $ctp-text;
        scrollbar-size-vertical: 1;
    }
    PopupMenu:focus { border: round $ctp-blue; }
    PopupMenu > .option-list--option-highlighted {
        background: $ctp-surface0; color: $ctp-text; text-style: bold;
    }
    PopupMenu > .option-list--option-hover {
        background: $ctp-surface1; color: $ctp-text;
    }
    """

    def __init__(
        self,
        choices: list[tuple[str, str]],
        value: str = "",
        *,
        anchor: Widget | None = None,
        position: Offset | None = None,
    ) -> None:
        self.choices = choices
        self._value, self._anchor, self._position = value, anchor, position
        self._previous_focus: Widget | None = None
        self._result: asyncio.Future[str | None] | None = None
        self._callback: Callable[[str | None], Awaitable[None] | None] | None = None
        self._viewport: Size | None = None
        self._dismissed = False
        super().__init__(*(Option(Text(label), id=value) for label, value in choices))

    def open(
        self,
        owner: Widget,
        callback: Callable[[str | None], Awaitable[None] | None] | None = None,
    ) -> None:
        for menu in owner.screen.query(PopupMenu):
            menu.dismiss()
        self._previous_focus = self._anchor or cast("Any", owner).app.focused
        self._anchor = self._anchor or self._previous_focus or owner
        self._callback = callback
        self._result = asyncio.get_running_loop().create_future()
        owner.screen.mount(self)

    async def choose(self, owner: Widget) -> str | None:
        self.open(owner)
        assert self._result is not None
        try:
            return await self._result
        finally:
            self.dismiss()

    def on_mount(self) -> None:
        if self._dismissed:
            self.remove()
            return
        self._place()
        signal: Signal[Screen[Any]] = cast("Any", self.screen).screen_layout_refresh_signal
        signal.subscribe(self, self._place)
        self.highlighted = next(
            (index for index, (_, value) in enumerate(self.choices) if value == self._value), 0
        )
        self.focus()
        self.capture_mouse()

    def _place(self, _screen: Screen[Any] | None = None) -> None:
        size = self.screen.size
        if size == self._viewport:
            return
        self._viewport = size
        width = min(
            max(22, max((cell_len(label) for label, _ in self.choices), default=0) + 4), size.width
        )
        height = min(len(self.choices) + 2, 16, size.height)
        self.styles.width, self.styles.height = width, height
        region = self._anchor.region if self._anchor is not None else self.screen.region
        x, y = self._position or Offset(region.x, region.bottom)
        if y + height > size.height:
            y = max(0, region.y - height) if self._position is None else size.height - height
        self.styles.offset = Offset(
            max(0, min(x, size.width - width)), max(0, min(y, size.height - height))
        )

    def on_blur(self) -> None:
        self.dismiss(restore_focus=False)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        if not self.region.contains(*event.screen_offset):
            event.prevent_default()
            self.dismiss()
        elif event.button != 1:
            event.prevent_default()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list is self:
            event.stop()
            self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss()

    def dismiss(self, value: str | None = None, *, restore_focus: bool = True) -> None:
        if self._dismissed:
            return
        self._dismissed = True
        if self.is_mounted:
            self.release_mouse()
            self.remove()
            if (
                restore_focus
                and self._previous_focus is not None
                and self._previous_focus.is_attached
            ):
                self._previous_focus.focus()
            if self._callback is not None:
                cast("Any", self).app.call_later(self._callback, value)
        if self._result is not None and not self._result.done():
            self._result.set_result(value)

    def on_unmount(self) -> None:
        signal: Signal[Screen[Any]] = cast("Any", self.screen).screen_layout_refresh_signal
        signal.unsubscribe(self)
        self.dismiss(restore_focus=False)


class ChoicePicker(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Any]] = [("escape", "back", "Back")]
    DEFAULT_CSS = """
    ChoicePicker { align: center middle; }
    ChoicePicker > Vertical { width: 70; max-width: 96%; height: 90%; border: round $accent; background: $surface; padding: 0 1; }
    ChoicePicker Static { height: auto; }
    ChoicePicker DataTable { height: 1fr; }
    """

    def __init__(
        self,
        service: Any,
        choices: list[tuple[str, str]],
        value: str = "",
        *,
        title: str = "",
    ) -> None:
        super().__init__()
        self.service, self.choices, self.value = service, choices, value
        self.title_text = title
        self.matches: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text or self.service.t("interface.browse"))
            yield Input(
                placeholder=self.service.t("tui.settings_search_placeholder"), id="picker-query"
            )
            yield DataTable(id="picker-results", cursor_type="row")
            with Horizontal():
                yield Button(self.service.t("tui.btn_apply"), id="picker-apply", variant="primary")
                yield Button(self.service.t("tui.btn_back"), id="picker-back")

    def on_mount(self) -> None:
        table = cast("DataTable[str]", self.query_one(DataTable))
        table.add_column("")
        self.filter()
        for index, (_, value) in enumerate(self.matches):
            if value == self.value:
                table.move_cursor(row=index)
                break

    def filter(self) -> None:
        query = self.query_one(Input).value.casefold()
        self.matches = [
            (label, value)
            for label, value in self.choices
            if query in f"{label} {value}".casefold()
        ]
        table = cast("DataTable[str]", self.query_one(DataTable))
        table.clear()
        for label, _ in self.matches:
            table.add_row(label)

    def on_input_changed(self, event: Input.Changed) -> None:
        if self.is_mounted:
            self.filter()

    def on_data_table_row_selected(self, event: Any) -> None:
        self.choose()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.choose()

    def choose(self) -> None:
        table = cast("DataTable[str]", self.query_one(DataTable))
        self.dismiss(
            self.matches[table.cursor_row][1] if table.row_count else self.query_one(Input).value
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "picker-apply":
            self.choose()
        else:
            self.dismiss(None)

    def action_back(self) -> None:
        self.dismiss(None)


class PathPicker(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Any]] = [("escape", "back", "Back")]
    DEFAULT_CSS = """
    PathPicker { align: center middle; }
    PathPicker > Vertical { width: 90; max-width: 96%; height: 92%; border: round $accent; background: $surface; padding: 0 1; }
    PathPicker DirectoryTree { height: 1fr; }
    PathPicker Static { height: auto; }
    """

    def __init__(self, service: Any, value: str, *, directory: bool = False) -> None:
        super().__init__()
        self.service, self.value, self.directory = service, value, directory

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                self.service.t(
                    "interface.choose_directory" if self.directory else "interface.choose_file"
                )
            )
            yield DirectoryTree(Path.home(), id="path-tree")
            yield Input(self.value or str(Path.home()), id="path-value")
            with Horizontal():
                yield Button(self.service.t("interface.parent_directory"), id="path-parent")
                yield Button(self.service.t("tui.btn_apply"), id="path-apply", variant="primary")
                yield Button(self.service.t("tui.btn_back"), id="path-back")

    async def on_mount(self) -> None:
        def root() -> Path:
            path = Path(self.value).expanduser() if self.value else Path.home()
            while not path.is_dir() and path != path.parent:
                path = path.parent
            return path

        self.query_one(DirectoryTree).path = await asyncio.to_thread(root)

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        event.stop()
        if not self.directory:
            self.query_one(Input).value = str(event.path)

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        event.stop()
        self.query_one(Input).value = str(event.path)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "path-parent":
            tree = self.query_one(DirectoryTree)
            tree.path = Path(tree.path).parent
            self.query_one(Input).value = str(tree.path)
            return
        self.dismiss(self.query_one(Input).value if event.button.id == "path-apply" else None)

    def action_back(self) -> None:
        self.dismiss(None)


class DatePicker(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Any]] = [("escape", "back", "Back")]
    DEFAULT_CSS = """
    DatePicker { align: center middle; }
    DatePicker ContentDialog { width: 64; }
    DatePicker Select { height: 3; width: 1fr; }
    """

    def __init__(self, service: Any, value: str, *, with_time: bool = False) -> None:
        super().__init__()
        self.service, self.with_time = service, with_time
        self.now = datetime.now(ZoneInfo(service.config.general.timezone))
        try:
            self.value = datetime.fromisoformat(value)
        except ValueError:
            self.value = self.now.replace(hour=9, minute=0)

    def compose(self) -> ComposeResult:
        with ContentDialog():
            yield Static(self.service.t("interface.choose_date"), classes="dialog-header")
            with Vertical(classes="dialog-body"):
                yield Static(
                    self.service.t("interface.timezone", zone=self.service.config.general.timezone),
                    classes="dialog-description",
                )
                for name, low, high, value in (
                    (
                        "year",
                        min(self.now.year - 5, self.value.year),
                        max(self.now.year + 10, self.value.year),
                        self.value.year,
                    ),
                    ("month", 1, 12, self.value.month),
                    ("day", 1, 31, self.value.day),
                ):
                    with Vertical(classes="dialog-section"):
                        yield Static(self.service.t("interface." + name))
                        yield SafeSelect(
                            [(str(v), str(v)) for v in range(low, high + 1)],
                            value=str(value),
                            allow_blank=False,
                            id="date-" + name,
                        )
                if self.with_time:
                    yield Static(self.service.t("interface.time"))
                    with Horizontal():
                        yield SafeSelect(
                            [(f"{v:02}", str(v)) for v in range(24)],
                            value=str(self.value.hour),
                            allow_blank=False,
                            id="date-hour",
                        )
                        yield SafeSelect(
                            [(f"{v:02}", str(v)) for v in range(60)],
                            value=str(self.value.minute),
                            allow_blank=False,
                            id="date-minute",
                        )
            with Vertical(classes="dialog-footer"):
                with Horizontal():
                    yield Button(self.service.t("interface.today"), id="date-today")
                    yield Button(self.service.t("interface.tomorrow"), id="date-tomorrow")
                    yield Button(self.service.t("interface.clear"), id="date-clear")
                with Horizontal(classes="dialog-section"):
                    yield Button(
                        self.service.t("tui.btn_apply"), id="date-apply", variant="primary"
                    )
                    yield Button(self.service.t("tui.btn_back"), id="date-back")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        ident = event.button.id
        if ident == "date-back":
            self.dismiss(None)
            return
        if ident == "date-clear":
            self.dismiss("")
            return
        if ident in {"date-today", "date-tomorrow"}:
            result = self.now + timedelta(days=int(ident == "date-tomorrow"))
        else:
            parts = {
                name: int(str(cast(Select[str], self.query_one("#date-" + name, Select)).value))
                for name in ("year", "month", "day")
            }
            parts["day"] = min(parts["day"], calendar.monthrange(parts["year"], parts["month"])[1])
            result = self.now.replace(year=parts["year"], month=parts["month"], day=parts["day"])
        if self.with_time:
            result = result.replace(
                hour=int(str(cast(Select[str], self.query_one("#date-hour", Select)).value)),
                minute=int(str(cast(Select[str], self.query_one("#date-minute", Select)).value)),
            )
        self.dismiss(result.strftime("%Y-%m-%d %H:%M" if self.with_time else "%Y-%m-%d"))

    def action_back(self) -> None:
        self.dismiss(None)


class AssistedInput(Horizontal):
    """A normal editable input with an explicit picker button beside it."""

    DEFAULT_CSS = """
    AssistedInput { height: 3; }
    AssistedInput Input { width: 1fr; }
    AssistedInput Button { width: auto; min-width: 8; margin: 0; }
    """

    def __init__(
        self,
        service: Any,
        value: str = "",
        *,
        id: str,
        mode: str = "file",
        choices: list[tuple[str, str]] | None = None,
        placeholder: str = "",
        classes: str = "",
    ) -> None:
        super().__init__()
        self.service, self.value, self.input_id = service, value, id
        self.mode, self.choices, self.placeholder, self.input_classes = (
            mode,
            choices or [],
            placeholder,
            classes,
        )

    def compose(self) -> ComposeResult:
        yield Input(
            self.value, id=self.input_id, placeholder=self.placeholder, classes=self.input_classes
        )
        yield Button(self.service.t("interface.browse"), id=self.input_id + "-browse")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        node = self.query_one(Input)
        if node.disabled:
            return
        screen: ModalScreen[str | None]
        if self.mode in {"date", "datetime"}:
            screen = DatePicker(self.service, node.value, with_time=self.mode == "datetime")
        elif self.mode == "choice":
            screen = ChoicePicker(self.service, self.choices, node.value)
        else:
            screen = PathPicker(self.service, node.value, directory=self.mode == "directory")

        def selected(value: str | None) -> None:
            if value is not None and node.is_attached:
                node.value = value
                node.focus()

        cast(Any, self).app.push_screen(screen, selected)
