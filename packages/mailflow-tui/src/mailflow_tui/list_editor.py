"""Interactive list editor: one input + add button, items with delete.

Used for the bot admin list (QQ number / wxid per item) and any other
line-list option: instead of a raw multiline text area the user gets an
input, an add button and the current items each with a remove button.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Input, Label


class ListEditor(Widget):
    """Edits a list of strings stored in ``self.items``."""

    DEFAULT_CSS = """
    ListEditor {
        height: auto;
        margin-bottom: 1;
    }
    #list-editor-input-row {
        height: 1;
        margin-bottom: 1;
    }
    #list-editor-input {
        height: 1;
    }
    #list-editor-add {
        height: 1;
        min-height: 1;
        padding: 0 2;
        margin: 0 0 0 1;
        border: none;
    }
    #list-editor-items {
        height: auto;
    }
    .list-editor-item {
        height: 1;
        margin-bottom: 0;
    }
    .list-editor-item-label {
        height: 1;
        padding: 0 1;
    }
    .list-editor-item-del {
        height: 1;
        min-height: 1;
        padding: 0 1;
        margin: 0 0 0 1;
        border: none;
        background: $error;
        color: $text;
    }
    """

    def __init__(self, items: list[str], placeholder: str = "", id: str | None = None) -> None:
        super().__init__(id=id)
        self.items = list(items)
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Horizontal(id="list-editor-input-row"):
            yield Input(placeholder=self._placeholder, id="list-editor-input")
            yield Button("+", id="list-editor-add", variant="success")
        with Vertical(id="list-editor-items"):
            yield from self._item_widgets()

    def _item_widgets(self) -> ComposeResult:
        for index, item in enumerate(self.items):
            with Horizontal(classes="list-editor-item"):
                yield Label(item, classes="list-editor-item-label")
                yield Button("x", id=f"list-editor-del-{index}", classes="list-editor-item-del")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "list-editor-add":
            input_box = self.query_one("#list-editor-input", Input)
            value = (input_box.value or "").strip()
            if value and value not in self.items:
                self.items.append(value)
                await self._refresh()
                input_box.value = ""
                input_box.focus()  # pyright: ignore[reportUnknownMemberType]
            return
        if button_id.startswith("list-editor-del-"):
            index = int(button_id[len("list-editor-del-") :])
            if 0 <= index < len(self.items):
                self.items.pop(index)
                await self._refresh()

    async def _refresh(self) -> None:
        container = self.query_one("#list-editor-items", Vertical)
        await container.remove_children()
        await container.mount_all(list(self._item_widgets()))

    def value(self) -> list[str]:
        """Current items (drops any leftover text in the input)."""
        return list(self.items)
