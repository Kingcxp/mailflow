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

from rich.segment import Segment
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, Input


class CenteredInput(Input):
    """An Input whose value text is centered while not focused.

    Textual's native Input renders its value left-aligned and ignores
    ``text-align`` / ``content-align`` (render_line hardcodes left). This
    subclass centers the rendered value when the field is not focused; while
    focused it delegates to the parent so the cursor, selection and scroll
    behave exactly as in a normal input. The placeholder stays left-aligned
    in both states so the hint never jumps.
    """

    def render_line(self, y: int) -> Strip:
        if y != 0 or self.has_focus or not self.value:
            # focused (cursor/selection/scroll native) and empty
            # (placeholder) rows are rendered by the parent, left-aligned
            return super().render_line(y)
        console = self.app.console  # pyright: ignore[reportUnknownMemberType]
        options = self.app.console_options  # pyright: ignore[reportUnknownMemberType]
        width = self.scrollable_content_region.width
        text = self._value
        pad = max(0, (width - text.cell_len) // 2)
        base = list(console.render(text, options.update_width(width - pad)))
        segs = [Segment(" " * pad, self.rich_style), *base]
        # apply_style gives every segment a style (Monochrome filter crashes
        # on None styles) and matches what Input.render_line does at the end
        strip = Strip(segs).extend_cell_length(width)
        return strip.apply_style(self.rich_style)


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
        height: 1;
        margin-bottom: 0;
        align-vertical: middle;
    }
    .list-editor-row Input {
        height: 1;
        border: none;
        padding: 0 1;
        width: 1fr;
    }
    .list-editor-row Button {
        height: 1;
        min-height: 1;
        width: 8;
        min-width: 8;
        padding: 0 1;
        margin: 0 0 0 1;
        border: none;
        content-align: center middle;
    }
    #list-editor-add {
        width: 100%;
        height: 1;
        min-height: 1;
        margin-top: 1;
        border: none;
        content-align: center middle;
    }
    """

    def __init__(
        self,
        items: list[str],
        *,
        placeholder: str = "",
        add_label: str,
        remove_label: str,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.items = [item for item in items if item]
        self._placeholder = placeholder
        self._add_label = add_label
        self._remove_label = remove_label
        self._row_tokens: list[str] = []
        self._row_refs: dict[str, Horizontal] = {}
        self._input_refs: dict[str, CenteredInput] = {}
        self._next_token = 0

    def _row(self, token: str, item: str) -> Horizontal:
        input_widget = CenteredInput(
            value=item, id=f"list-editor-input-{token}", placeholder=self._placeholder
        )
        row = Horizontal(
            input_widget,
            Button(self._remove_label, id=f"list-editor-del-{token}", variant="error"),
            classes="list-editor-row",
        )
        self._row_refs[token] = row
        self._input_refs[token] = input_widget
        return row

    def _token(self) -> str:
        token = str(self._next_token)
        self._next_token += 1
        self._row_tokens.append(token)
        return token

    def compose(self) -> ComposeResult:
        with Vertical(id="list-editor-rows"):
            rows = self.items if self.items else [""]
            for item in rows:
                yield self._row(self._token(), item)
        yield Button(self._add_label, id="list-editor-add", variant="success")

    def _all_row_values(self) -> list[str]:
        """Every row's typed value (empty rows included)."""
        return [self._input_refs[token].value for token in self._row_tokens]

    def _current_values(self) -> list[str]:
        return [value.strip() for value in self._all_row_values() if value.strip()]

    async def _append_row(self, value: str = "") -> CenteredInput:
        token = self._token()
        row = self._row(token, value)
        await self.query_one("#list-editor-rows", Vertical).mount(row)
        return self._input_refs[token]

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if (event.input.id or "") not in {
            f"list-editor-input-{token}" for token in self._row_tokens
        }:
            return
        self.items = self._current_values()
        (await self._append_row()).focus()  # pyright: ignore[reportUnknownMemberType]

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "list-editor-add":
            self.items = self._current_values()
            (await self._append_row()).focus()  # pyright: ignore[reportUnknownMemberType]
            return
        if not button_id.startswith("list-editor-del-"):
            return
        token = button_id[len("list-editor-del-") :]
        if token not in self._row_tokens:
            return
        index = self._row_tokens.index(token)
        row = self._row_refs.pop(token)
        self._input_refs.pop(token)
        self._row_tokens.pop(index)
        await row.remove()
        if not self._row_tokens:
            new_input = await self._append_row()
        else:
            new_input = self._input_refs[self._row_tokens[min(index, len(self._row_tokens) - 1)]]
        self.items = self._current_values()
        new_input.focus()  # pyright: ignore[reportUnknownMemberType]

    def value(self) -> list[str]:
        """Current non-empty items (what is typed in the rows)."""
        return self._current_values()
