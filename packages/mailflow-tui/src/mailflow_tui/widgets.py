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

from typing import Any, ClassVar, cast

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.css.query import QueryError
from textual.message import Message
from textual.widgets import Button, DataTable, Select
from textual.widgets._select import SelectCurrent, SelectOverlay


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


__all__ = ["SafeSelect", "SearchToggle", "SelectionTable"]


class SelectionTable(DataTable[Any]):
    """Checked rows are independent of the row being read or edited."""

    BINDINGS: ClassVar[list[Any]] = [
        Binding("space", "toggle_check", show=False),
        Binding("ctrl+a", "toggle_all", show=False),
        Binding("escape", "clear_checks", show=False),
        Binding("shift+up", "extend_selection(-1)", show=False),
        Binding("shift+down", "extend_selection(1)", show=False),
    ]

    class SelectionChanged(Message):
        def __init__(self, table: SelectionTable) -> None:
            super().__init__()
            self.table = table

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.checked_keys: set[str] = set()
        self._anchor: str | None = None

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
        elif row >= 0 and (column == 0 or event.ctrl or event.shift):
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
