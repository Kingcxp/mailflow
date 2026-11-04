"""Select subclass that survives Textual's mount/remount races.

Two crash paths in upstream ``Select`` (both still unguarded in Textual
main) hit the same query: ``SelectCurrent.update()`` asks for its internal
``#label`` child and raises ``NoMatches`` when it is missing.

1. Mount order: ``Select._on_mount`` applies the initial value; on slow
   runners ``SelectCurrent``'s children may not be composed yet.
2. Remount teardown: the reactive ``value`` watcher can run on a refresh
   cycle AFTER the pane holding the Select was detached (language-switch
   remount), so the label is gone by the time the watcher fires.

``SafeSelect`` defers the initial application and replicates the watcher
body minus the crash; losing a label update on a detached Select is
harmless because the widget's display is irrelevant once removed.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any, ClassVar, cast

from mailflow.i18n import I18n
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.css.query import QueryError
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Select, Static
from textual.widgets import Input as TextualInput
from textual.widgets import TextArea as TextualTextArea
from textual.widgets._select import SelectCurrent, SelectOverlay
from textual.widgets.input import Selection


def _label_ready(select: Select[object]) -> bool:
    """Whether the Select's current-value label finished composing."""
    try:
        return bool(select.query("SelectCurrent #label"))
    except QueryError:
        return False


class SafeSelect(Select[object]):  # pyright: ignore[reportUnsafeMultipleInheritance]
    """A :class:`Select` that never lets its value watcher crash on a
    missing ``#label``; everything else behaves exactly like ``Select``."""

    def _init_selected_option(self, hint: object = Select.NULL) -> None:
        if hint == Select.NULL and not self._allow_blank:
            hint = self._options[0][1]
        if hint != self._value and not _label_ready(self):
            # still composing: retry on the next frame rather than poking a
            # half-built SelectCurrent (its update() queries '#label')
            self.call_after_refresh(self._init_selected_option, hint)
            return
        self.value = hint  # pyright: ignore[reportUnknownMemberType]

    def _watch_value(self, value: object) -> None:
        """Textual's watcher body, guarded against a missing ``#label``."""
        self._value = value  # pyright: ignore[reportUnknownMemberType]
        try:
            select_current = self.query_one(SelectCurrent)
            select_overlay = self.query_one(SelectOverlay)
            if not select_current.query("#label"):
                return
        except QueryError:
            return
        if value == Select.NULL:
            select_current.update(Select.NULL)
        else:
            for index, (_prompt, option_value) in enumerate(self._options):
                if option_value == value:
                    select_overlay.highlighted = index
                    select_current.update(cast(Any, _prompt))
                    break
        changed = cast(Any, Select.Changed)(self, cast(Any, value))
        self.post_message(changed)


__all__ = [
    "Input",
    "SafeSelect",
    "SearchToggle",
    "SecretRow",
    "SelectionTable",
    "ShortcutHelp",
    "TextArea",
]


class Input(TextualInput):
    """Desktop editing keys and local undo, shared by every single-line field."""

    BINDINGS: ClassVar[list[Any]] = [
        Binding("ctrl+a,f7", "select_all", show=False),
        Binding("ctrl+z", "undo", show=False),
        Binding("ctrl+y,ctrl+shift+z", "redo", show=False),
        Binding("ctrl+insert", "copy", show=False),
        Binding("shift+insert", "paste", show=False),
        Binding("shift+delete", "cut", show=False),
        Binding("ctrl+backspace", "delete_left_word", show=False),
        Binding("ctrl+delete", "delete_right_word", show=False),
    ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._undo: deque[tuple[str, Selection]] = deque(maxlen=100)
        self._redo: deque[tuple[str, Selection]] = deque(maxlen=100)
        self._editing = False
        super().__init__(*args, **kwargs)

    def _watch_value(self, value: str) -> None:
        if not self._editing:
            # Loading another account/provider must not undo into the previous field value.
            self._undo.clear()
            self._redo.clear()
        super()._watch_value(value)

    def replace(self, text: str, start: int, end: int) -> None:
        previous = (self.value, self.selection)
        self._editing = True
        try:
            super().replace(text, start, end)
        finally:
            self._editing = False
        if self.value != previous[0]:
            self._undo.append(previous)
            self._redo.clear()

    def _restore_edit(
        self,
        source: deque[tuple[str, Selection]],
        target: deque[tuple[str, Selection]],
    ) -> None:
        if not source:
            return
        target.append((self.value, self.selection))
        value, selection = source.pop()
        self._editing = True
        try:
            self.value = value
            self.selection = selection
        finally:
            self._editing = False

    def action_undo(self) -> None:
        self._restore_edit(self._undo, self._redo)

    def action_redo(self) -> None:
        self._restore_edit(self._redo, self._undo)

    def action_copy(self) -> None:
        # Consume the key even without a selection; it must not reach App's quit action.
        if self.selected_text and not self.password:
            self.app.copy_to_clipboard(self.selected_text)  # pyright: ignore[reportUnknownMemberType]

    def action_cut(self) -> None:
        if self.selected_text and not self.password:
            self.app.copy_to_clipboard(self.selected_text)  # pyright: ignore[reportUnknownMemberType]
            self.delete_selection()

    def action_paste(self) -> None:
        if self.app.clipboard:  # pyright: ignore[reportUnknownMemberType]
            super().action_paste()


class TextArea(TextualTextArea):
    """Use the same editing shortcuts for replies, profiles and multiline settings."""

    BINDINGS: ClassVar[list[Any]] = [
        Binding("ctrl+a", "select_all", show=False),
        Binding("ctrl+shift+z", "redo", show=False),
        Binding("ctrl+insert", "copy", show=False),
        Binding("shift+insert", "paste", show=False),
        Binding("shift+delete", "cut", show=False),
    ]

    def action_copy(self) -> None:
        if self.selected_text:
            self.app.copy_to_clipboard(self.selected_text)  # pyright: ignore[reportUnknownMemberType]

    def action_paste(self) -> None:
        if self.app.clipboard:  # pyright: ignore[reportUnknownMemberType]
            super().action_paste()


class ShortcutHelp(ModalScreen[None]):
    """A scrollable help sheet that also works without mouse hover or modifiers."""

    BINDINGS: ClassVar[list[Any]] = [Binding("escape,f1", "dismiss", show=False)]
    DEFAULT_CSS = """
    ShortcutHelp { align: center middle; }
    ShortcutHelp > Vertical {
        width: 76; max-width: 95%; height: 85%;
        border: round $primary; padding: 1 2; background: $surface;
    }
    ShortcutHelp ScrollableContainer { height: 1fr; }
    ShortcutHelp #shortcut-title { text-style: bold; margin-bottom: 1; }
    ShortcutHelp Button { width: 100%; margin-top: 1; }
    """

    def __init__(self, t: Callable[..., str]) -> None:
        super().__init__()
        self._t = t

    def compose(self) -> ComposeResult:
        table = Table.grid(padding=(0, 2), expand=True)
        table.add_column(width=22, style="bold cyan")
        table.add_column(ratio=1)
        for key, description in (
            ("Tab / Shift+Tab", "help_focus"),
            ("Enter / Space", "help_activate"),
            ("Ctrl+A / F7", "help_edit_select"),
            ("Ctrl+C / Ctrl+Insert", "help_edit_copy"),
            ("Ctrl+V / Shift+Insert", "help_edit_paste"),
            ("Ctrl+X / Shift+Delete", "help_edit_cut"),
            ("Ctrl+Z / Ctrl+Y", "help_edit_history"),
            ("↑ / ↓ / PageUp / PageDown", "help_rows"),
            ("Space", "selection_toggle"),
            ("F2", "help_selection_mode"),
            ("F3", "help_selection_range"),
            ("Ctrl+A / F4", "help_selection_all"),
            ("Esc", "help_selection_clear"),
            ("Ctrl+Z", "help_mail_undo"),
        ):
            table.add_row(key, self._t(f"tui.{description}"))
        with Vertical():
            yield Static(self._t("tui.help_shortcuts"), id="shortcut-title", markup=False)
            with ScrollableContainer():
                yield Static(table)
                yield Static(self._t("sessions.help"), markup=False)
                yield Static(self._t("tui.help_terminal_clipboard"), markup=False)
            yield Button(self._t("tui.btn_close"), id="shortcut-close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss()


class SecretRow(Horizontal):
    """Place the visibility control inside a full-width password input."""

    def on_mount(self) -> None:
        self._place_toggle()

    def on_resize(self) -> None:
        self._place_toggle()

    def _place_toggle(self) -> None:
        for button in self.query(Button):
            button.active_effect_duration = 0
            button.styles.offset = (max(0, self.size.width - 7), 1)


class SelectionTable(DataTable[Any]):
    """Checked rows are independent of the row being read or edited."""

    BINDINGS: ClassVar[list[Any]] = [
        Binding("space", "toggle_check", show=False),
        Binding("ctrl+a,f4", "toggle_all", show=False),
        Binding("escape", "clear_checks", show=False),
        Binding("shift+up", "extend_selection(-1)", show=False),
        Binding("shift+down", "extend_selection(1)", show=False),
        Binding("f2", "selection_mode", show=False),
        Binding("f3", "select_range", show=False),
    ]

    class SelectionChanged(Message):
        def __init__(self, table: SelectionTable) -> None:
            super().__init__()
            self.table = table

    def __init__(self, *, t: Callable[..., str] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._t = t or I18n().t
        self.checked_keys: set[str] = set()
        self._anchor: str | None = None
        self.selection_mode = False

    def on_mount(self) -> None:
        self.relabel()

    def on_focus(self) -> None:
        self.relabel()

    def relabel(self) -> None:
        self._bindings.bind("f2", "selection_mode", self._t("tui.selection_mode"))
        self._bindings.bind("f3", "select_range", self._t("tui.selection_range"))
        self.tooltip = self._t("tui.selection_help")

    def action_selection_mode(self) -> None:
        self.selection_mode = not self.selection_mode
        self.post_message(self.SelectionChanged(self))

    def action_select_range(self) -> None:
        key = self._key_at(self.cursor_row)
        if key is not None:
            self._check_range(key)

    def check_label(self, key: str) -> Text:
        return Text("[x]" if key in self.checked_keys else "[ ]", style="bold cyan")

    def visible_keys(self) -> list[str]:
        return [str(row.key.value) for row in self.ordered_rows]

    def retain_checks(self, available: set[str]) -> None:
        self.set_checks(self.checked_keys & available)

    def set_checks(self, keys: set[str]) -> None:
        if keys == self.checked_keys:
            return
        self.checked_keys = set(keys)
        for key in self.visible_keys():
            self.update_cell(key, "selected", self.check_label(key))
        self.post_message(self.SelectionChanged(self))

    def _key_at(self, row: int) -> str | None:
        keys = self.visible_keys()
        return keys[row] if 0 <= row < len(keys) else None

    def _check_range(self, key: str) -> None:
        keys = self.visible_keys()
        if self._anchor not in keys:
            self._anchor = key
        start, end = sorted((keys.index(self._anchor), keys.index(key)))
        self.set_checks(self.checked_keys | set(keys[start : end + 1]))

    def action_toggle_check(self) -> None:
        key = self._key_at(self.cursor_row)
        if key is not None:
            self._anchor = key
            self.set_checks(self.checked_keys ^ {key})

    def action_toggle_all(self) -> None:
        visible = set(self.visible_keys())
        self.set_checks(
            self.checked_keys - visible
            if visible <= self.checked_keys
            else self.checked_keys | visible
        )

    def action_clear_checks(self) -> None:
        self._anchor = None
        self.set_checks(set())

    def action_extend_selection(self, direction: int) -> None:
        if not self.row_count:
            return
        if self._anchor is None:
            self._anchor = self._key_at(self.cursor_row)
        self.move_cursor(row=max(0, min(self.row_count - 1, self.cursor_row + direction)))
        key = self._key_at(self.cursor_row)
        if key is not None:
            self._check_range(key)

    def on_click(self, event: events.Click) -> None:
        meta = event.style.meta
        row, column = meta.get("row"), meta.get("column")
        if event.button != 1 or not isinstance(row, int):
            return
        if row == -1 and column == 0:
            self.action_toggle_all()
        elif row >= 0 and (column == 0 or event.ctrl or event.shift or self.selection_mode):
            key = self._key_at(row)
            if key is None:
                return
            if event.shift and self._anchor is None:
                self._anchor = self._key_at(self.cursor_row)
            self.move_cursor(row=row)
            if event.shift:
                self._check_range(key)
            else:
                self.action_toggle_check()
        else:
            return
        self.focus()
        event.stop()
        event.prevent_default()


class SearchToggle(Button):
    """Compact on/off toggle for the search inputs (``r*`` = regex, ``Aa``
    = case-sensitive).

    A labelled Checkbox eats a third of the search row; these two-letter
    toggles read at a glance: idle grey, active green, pressed once to
    flip. Exposes the same ``value`` attribute a Checkbox had so the
    matcher code does not change.
    """

    LABELS: ClassVar[dict[str, str]] = {"regex": "r*", "case": "Aa"}

    class Toggled(Message):
        """Posted after the toggle flips; bubbles like Checkbox.Changed did."""

        def __init__(self, toggle: SearchToggle, value: bool) -> None:
            super().__init__()
            self.toggle = toggle
            self.value = value

        @property
        def control(self) -> SearchToggle:
            return self.toggle

    def __init__(self, kind: str, *, ident: str) -> None:
        super().__init__(self.LABELS.get(kind, kind), id=ident, variant="default")
        self._kind = kind
        self._on = False

    @property
    def value(self) -> bool:
        """Current toggle state (Checkbox-compatible reads)."""
        return self._on

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._on = not self._on
        self.variant = "success" if self._on else "default"
        self.post_message(self.Toggled(self, self._on))
