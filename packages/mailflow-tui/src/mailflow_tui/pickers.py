"""Optional selection helpers; the underlying text fields stay editable."""

from __future__ import annotations

import asyncio
import calendar
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, cast
from zoneinfo import ZoneInfo

from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, DirectoryTree, Select, Static

from mailflow_tui.widgets import Input, SafeSelect


class ChoicePicker(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Any]] = [("escape", "back", "Back")]
    DEFAULT_CSS = """
    ChoicePicker { align: center middle; }
    ChoicePicker > Vertical { width: 70; max-width: 96%; height: 90%; border: round $accent; background: $surface; padding: 0 1; }
    ChoicePicker Static { height: auto; }
    ChoicePicker DataTable { height: 1fr; }
    """

    def __init__(self, service: Any, choices: list[tuple[str, str]], value: str = "") -> None:
        super().__init__()
        self.service, self.choices, self.value = service, choices, value
        self.matches: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.service.t("interface.browse"))
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
    DatePicker > Vertical { width: 64; max-width: 96%; height: 92%; max-height: 26; border: round $accent; background: $surface; padding: 0 1; }
    DatePicker ScrollableContainer { height: 1fr; }
    DatePicker Static { height: auto; }
    DatePicker Select { height: 3; width: 1fr; }
    DatePicker Horizontal { height: 3; }
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
        with Vertical():
            yield Static(self.service.t("interface.choose_date"))
            yield Static(
                self.service.t("interface.timezone", zone=self.service.config.general.timezone)
            )
            with ScrollableContainer():
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
            with Horizontal():
                yield Button(self.service.t("interface.today"), id="date-today")
                yield Button(self.service.t("interface.tomorrow"), id="date-tomorrow")
                yield Button(self.service.t("interface.clear"), id="date-clear")
            with Horizontal():
                yield Button(self.service.t("tui.btn_apply"), id="date-apply", variant="primary")
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
