"""Editable list editor: one input row per item, a delete button per row,
and a bottom add button. Enter in a row also appends a new row.

Used for the bot admin list (QQ number / wxid per item) and any other
line-list option. The rows are themselves inputs — whatever the user types
is part of the list immediately, so there is no hidden "add to readonly
list" step and a required field validates against what is actually typed.

Rows are built directly (children passed to the container constructor)
instead of the ``with Horizontal(...):`` compose context manager — that
manager reads ``app._compose_stacks`` which only exists during compose, so
rebuilding rows from a button handler would raise IndexError.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Input


class ListEditor(Widget):
    """Edits a list of strings; each item is its own editable input row."""

    DEFAULT_CSS = """
    ListEditor {
        height: auto;
        margin-bottom: 1;
    }
    #list-editor-rows {
        height: auto;
    }
    .list-editor-row {
        height: auto;
        margin-bottom: 0;
        align-vertical: middle;
    }
    .list-editor-row Input {
        height: 3;
        border: none;
        padding: 0 1;
        width: 1fr;
    }
    .list-editor-row Button {
        height: auto;
        min-height: 3;
        min-width: 8;
        padding: 0 1;
        margin: 0 0 0 1;
        border: none;
    }
    #list-editor-add {
        width: auto;
        min-width: 8;
        height: auto;
        margin-top: 1;
        border: none;
    }
    """

    def __init__(self, items: list[str], placeholder: str = "", id: str | None = None) -> None:
        super().__init__(id=id)
        # an empty list still shows one empty input row so the user can type
        # straight away; "" items are dropped from value()
        self.items = [item for item in items if item]
        self._placeholder = placeholder

    def _row(self, index: int, item: str) -> Horizontal:
        return Horizontal(
            Input(value=item, id=f"list-editor-input-{index}", placeholder=self._placeholder),
            Button("x", id=f"list-editor-del-{index}", variant="error"),
            classes="list-editor-row",
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="list-editor-rows"):
            rows = self.items if self.items else [""]
            for index, item in enumerate(rows):
                yield self._row(index, item)
        yield Button("+ add", id="list-editor-add", variant="success")

    async def _render_rows(self) -> None:
        container = self.query_one("#list-editor-rows", Vertical)
        await container.remove_children()
        rows = self.items if self.items else [""]
        for index, item in enumerate(rows):
            await container.mount(self._row(index, item))

    def _all_row_values(self) -> list[str]:
        """Every row's typed value (empty rows included)."""
        return [row.value for row in self.query(Input)]

    def _current_values(self) -> list[str]:
        """Non-empty typed values across all rows."""
        return [value.strip() for value in self._all_row_values() if value.strip()]

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in a row adds a new empty row below and focuses it."""
        input_id = event.input.id or ""
        if not input_id.startswith("list-editor-input-"):
            return
        # commit what is in this row, then open a fresh row beneath
        self.items = self._all_row_values()
        self.items.append("")
        await self._render_rows()
        new_index = len(self.items) - 1
        new_input = self.query_one(f"#list-editor-input-{new_index}", Input)
        new_input.focus()  # pyright: ignore[reportUnknownMemberType]

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "list-editor-add":
            self.items = self._all_row_values()
            self.items.append("")
            await self._render_rows()
            new_index = len(self.items) - 1
            new_input = self.query_one(f"#list-editor-input-{new_index}", Input)
            new_input.focus()  # pyright: ignore[reportUnknownMemberType]
            return
        if button_id.startswith("list-editor-del-"):
            index = int(button_id[len("list-editor-del-") :])
            rows = self._all_row_values()
            if 0 <= index < len(rows):
                rows.pop(index)
                self.items = rows if rows else [""]
                await self._render_rows()

    def value(self) -> list[str]:
        """Current non-empty items (what is typed in the rows)."""
        return self._current_values()
