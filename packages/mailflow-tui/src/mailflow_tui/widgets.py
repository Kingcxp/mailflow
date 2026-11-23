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
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical, VerticalScroll
from textual.css.query import QueryError
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Select, Static
from textual.widgets import Input as TextualInput
from textual.widgets import TextArea as TextualTextArea
from textual.widgets._select import SelectCurrent, SelectOverlay
from textual.widgets.input import Selection


class ContentDialog(VerticalScroll):
    """A content-sized modal frame with pinned title and action sections."""

    DEFAULT_CSS = """
    ContentDialog {
        width: 88; max-width: 95%; height: auto; max-height: 90%;
        background: $surface; border: round $accent; padding: 1 2;
        overflow-x: hidden; scrollbar-size-vertical: 1;
    }
    ContentDialog > .dialog-header {
        dock: top; height: auto; margin-bottom: 1;
        color: $accent; text-style: bold;
    }
    ContentDialog > .dialog-footer {
        dock: bottom; height: auto; padding-top: 1; background: $surface;
    }
    ContentDialog .dialog-body, ContentDialog .dialog-section { height: auto; }
    ContentDialog .dialog-section { margin-top: 1; }
    ContentDialog .dialog-description {
        height: auto; color: $text-muted; margin-bottom: 1;
    }
    ContentDialog Static { height: auto; }
    ContentDialog .dialog-status { height: auto; max-height: 4; overflow-y: auto; }
    ContentDialog Horizontal { height: auto; align-vertical: middle; }
    ContentDialog Input, ContentDialog Select { height: 3; }
    ContentDialog Button { width: 1fr; min-width: 0; margin-right: 1; }
    """


def _label_ready(select: Select[object]) -> bool:
    """Whether the Select's current-value label finished composing."""
    try:
        return bool(select.query("SelectCurrent #label"))
    except QueryError:
        return False


class StateButton(Button):
    """A compact boolean editor with an explicit, translated state."""

    class Changed(Message):
        def __init__(self, button: StateButton, value: bool) -> None:
            super().__init__()
            self.button = button
            self.value = value

        @property
        def control(self) -> StateButton:
            return self.button

    def __init__(
        self,
        t: Callable[..., str],
        *,
        value: bool = False,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        self._t = t
        self._value = value
        super().__init__(
            t("tui.plugin_status_enabled" if value else "tui.plugin_status_disabled"),
            variant="success" if value else "error",
            id=id,
            classes=classes,
        )

    @property
    def value(self) -> bool:
        return self._value

    @value.setter
    def value(self, value: bool) -> None:
        changed = self._value != value
        self._value = value
        self.relabel()
        if changed:
            self.post_message(self.Changed(self, value))

    def relabel(self) -> None:
        self.label = self._t(
            "tui.plugin_status_enabled" if self._value else "tui.plugin_status_disabled"
        )
        self.variant = "success" if self._value else "error"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.value = not self.value


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
        # set_options may keep the same value while translating its label.
        # Refresh the label without emitting a second selection change.
        self._render_value(hint)

    def _watch_value(self, value: object) -> None:
        """Textual's watcher body, guarded against a missing ``#label``."""
        self._value = value  # pyright: ignore[reportUnknownMemberType]
        if not self._render_value(value):
            return
        changed = cast(Any, Select.Changed)(self, cast(Any, value))
        self.post_message(changed)

    def _render_value(self, value: object) -> bool:
        try:
            select_current = self.query_one(SelectCurrent)
            select_overlay = self.query_one(SelectOverlay)
            if not select_current.query("#label"):
                return False
        except QueryError:
            return False
        if value == Select.NULL:
            select_current.update(Select.NULL)
        else:
            for index, (_prompt, option_value) in enumerate(self._options):
                if option_value == value:
                    select_overlay.highlighted = index
                    select_current.update(cast(Any, _prompt))
                    break
        return True


__all__ = [
    "Input",
    "PromptInput",
    "SafeSelect",
    "SearchToggle",
    "SelectionTable",
    "ShortcutHelp",
    "TextArea",
]


class Input(TextualInput):
    """Desktop editing keys and local undo, shared by every single-line field."""

    DEFAULT_CSS = "Input { height: 3; min-height: 1; }"

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
        Binding("ctrl+home", "cursor_document_start", show=False),
        Binding("ctrl+end", "cursor_document_end", show=False),
        Binding("ctrl+shift+home", "cursor_document_start(True)", show=False),
        Binding("ctrl+shift+end", "cursor_document_end(True)", show=False),
        Binding("ctrl+shift+z", "redo", show=False),
        Binding("ctrl+insert", "copy", show=False),
        Binding("shift+insert", "paste", show=False),
        Binding("shift+delete", "cut", show=False),
    ]

    def action_cursor_document_start(self, select: bool = False) -> None:
        self.move_cursor((0, 0), select=select)

    def action_cursor_document_end(self, select: bool = False) -> None:
        self.move_cursor(self.document.end, select=select)

    def action_copy(self) -> None:
        if self.selected_text:
            self.app.copy_to_clipboard(self.selected_text)  # pyright: ignore[reportUnknownMemberType]

    def action_paste(self) -> None:
        if self.app.clipboard:  # pyright: ignore[reportUnknownMemberType]
            super().action_paste()


class PromptInput(TextArea):
    """An input that grows to five visible text lines and submits with Enter."""

    DEFAULT_CSS = """
    PromptInput {
        height: 3; min-height: 3; max-height: 7;
        scrollbar-size-vertical: 1;
        overflow-x: hidden; overflow-y: auto;
    }
    """

    class Submitted(Message):
        def __init__(self, prompt: PromptInput) -> None:
            super().__init__()
            self.input = prompt
            self.value = prompt.value

    def __init__(self, value: str = "", **kwargs: Any) -> None:
        super().__init__(value, soft_wrap=True, tab_behavior="focus", **kwargs)

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, value: str) -> None:
        self.text = value

    def on_mount(self) -> None:
        self.call_after_refresh(self._fit_height)

    def on_resize(self, _event: events.Resize) -> None:
        self.call_after_refresh(self._fit_height)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area is self:
            self._fit_height()

    def _fit_height(self) -> None:
        lines = max(1, min(5, self.wrapped_document.height))
        height = lines + self.styles.gutter.height
        if self.size.height != height:
            self.styles.height = height
        self.call_after_refresh(self.scroll_cursor_visible)

    def on_key(self, event: events.Key) -> None:
        if event.key not in {"enter", "shift+enter"}:
            return
        event.stop()
        event.prevent_default()
        if self.read_only:
            return
        if event.key == "shift+enter":
            self.replace("\n", *self.selection, maintain_selection_offset=False)
        elif self.value.strip():
            self.post_message(self.Submitted(self))


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
    ShortcutHelp .shortcut-section-title {
        margin-top: 1; border-top: solid $primary; padding-top: 1;
        text-style: bold; color: $accent;
    }
    ShortcutHelp .shortcut-note { margin-top: 1; }
    ShortcutHelp Button { width: 100%; margin-top: 1; }
    """

    def __init__(self, t: Callable[..., str]) -> None:
        super().__init__()
        self._t = t

    def compose(self) -> ComposeResult:
        table = Table.grid(padding=(0, 2), expand=True)
        table.add_column(width=22, style="bold $ctp-blue")
        table.add_column(ratio=1)
        for key, description in (
            ("Ctrl+9", "help_console"),
            ("Tab / Shift+Tab", "help_focus"),
            ("Enter / Space", "help_activate"),
            ("Ctrl+A / F7", "help_edit_select"),
            ("Ctrl+C / Ctrl+Insert", "help_edit_copy"),
            ("Ctrl+V / Shift+Insert", "help_edit_paste"),
            ("Ctrl+X / Shift+Delete", "help_edit_cut"),
            ("Ctrl+Z / Ctrl+Y", "help_edit_history"),
            ("Enter / Shift+Enter", "help_prompt_submit"),
            ("↑ / ↓ / PageUp / PageDown", "help_rows"),
            (self._t("tui.selection_click"), "help_selection_single"),
            ("Space / Ctrl+Enter", "selection_toggle"),
            ("Ctrl+Click", "help_selection_toggle"),
            ("Shift+↑ / ↓ / Click", "help_selection_extend"),
            ("Ctrl+↑ / ↓", "help_selection_focus"),
            ("F3", "help_selection_range"),
            ("Ctrl+A", "help_selection_all"),
            ("Esc", "help_selection_clear"),
            ("Delete", "help_selection_delete"),
            ("M / Shift+F10", "help_selection_menu"),
            ("Ctrl+Z", "help_mail_undo"),
        ):
            table.add_row(key, self._t(f"tui.{description}"))
        with Vertical():
            yield Static(self._t("tui.help_shortcuts"), id="shortcut-title", markup=False)
            with ScrollableContainer():
                yield Static(table)
                for title, content in (
                    ("tui.help_sessions", "sessions.help"),
                    ("tui.help_clipboard", "tui.help_terminal_clipboard"),
                ):
                    yield Static(self._t(title), classes="shortcut-section-title", markup=False)
                    note = Text(self._t(content))
                    note.highlight_words(
                        (
                            "F8",
                            "Ctrl+Q",
                            "mailflow tui list",
                            "mailflow tui attach NAME",
                            "Ctrl+V",
                            "Shift+Insert",
                        ),
                        style="bold $ctp-blue",
                    )
                    yield Static(note, classes="shortcut-note")
            yield Button(self._t("tui.btn_close"), id="shortcut-close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss()


class SelectionTable(DataTable[Any]):
    """Shared row selection, with Explorer-style interaction for the main lists."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {"selectiontable--checked"}
    DEFAULT_CSS = """
    SelectionTable > .selectiontable--checked { background: $block-cursor-background; }
    """

    BINDINGS: ClassVar[list[Any]] = [
        Binding("space,ctrl+enter,ctrl+j", "toggle_check", show=False),
        Binding("ctrl+a,f4", "toggle_all", show=False),
        Binding("escape", "clear_checks", show=False),
        Binding("shift+up", "extend_selection(-1)", show=False),
        Binding("shift+down", "extend_selection(1)", show=False),
        Binding("f2", "selection_mode", show=False),
        Binding("f3", "select_range", show=False),
        Binding("m,shift+f10", "context_menu", show=False),
        Binding("ctrl+up", "focus_row(-1)", show=False),
        Binding("ctrl+down", "focus_row(1)", show=False),
        Binding("delete", "delete_selection", show=False),
    ]

    class SelectionChanged(Message):
        def __init__(self, table: SelectionTable) -> None:
            super().__init__()
            self.table = table

    class MenuRequested(Message):
        def __init__(self, table: SelectionTable) -> None:
            super().__init__()
            self.table = table

    class DeleteRequested(Message):
        def __init__(self, table: SelectionTable) -> None:
            super().__init__()
            self.table = table

    def __init__(
        self,
        *,
        t: Callable[..., str] | None = None,
        explorer_selection: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._t = t or I18n().t
        self.explorer_selection = explorer_selection
        self.checked_keys: set[str] = set()
        self.unselectable_keys: set[str] = set()
        self._anchor: str | None = None
        self._range_base: set[str] | None = None
        self.selection_mode = False

    def on_mount(self) -> None:
        self.relabel()

    def on_focus(self) -> None:
        self.relabel()

    def relabel(self) -> None:
        self._bindings.bind("f2", "selection_mode", self._t("tui.selection_mode"), show=False)
        self._bindings.bind("f3", "select_range", self._t("tui.selection_range"), show=False)
        self.tooltip = None

    def action_selection_mode(self) -> None:
        if self.explorer_selection:
            return
        self.selection_mode = not self.selection_mode
        self._range_base = None
        self.post_message(self.SelectionChanged(self))

    def action_context_menu(self) -> None:
        self.post_message(self.MenuRequested(self))

    def action_delete_selection(self) -> None:
        if self.explorer_selection and self.checked_keys:
            self.post_message(self.DeleteRequested(self))

    def action_select_range(self) -> None:
        key = self._key_at(self.cursor_row)
        if key is not None:
            self._check_range(key)

    def check_label(self, key: str) -> Text:
        if key in self.unselectable_keys:
            return Text("·", style="$mf-muted")
        return Text("[x]" if key in self.checked_keys else "[ ]", style="bold $ctp-blue")

    def visible_keys(self) -> list[str]:
        return [str(row.key.value) for row in self.ordered_rows]

    def retain_checks(self, available: set[str]) -> None:
        if self._range_base is not None:
            self._range_base.intersection_update(available)
        if self._anchor not in available:
            self._anchor = None
            self._range_base = None
        self._set_checks(self.checked_keys & available)

    def set_checks(self, keys: set[str]) -> None:
        self._range_base = None
        self._set_checks(keys)

    def restore_selection(self, *, select_initial: bool = False) -> None:
        if not self.explorer_selection:
            return
        if select_initial and not self.checked_keys:
            self._select_current_row()
        self.show_cursor = self._key_at(self.cursor_row) in self.checked_keys

    def _select_current_row(self) -> None:
        key = self._key_at(self.cursor_row)
        self._anchor = key
        self.set_checks({key} if key is not None else set())

    def _set_checks(self, keys: set[str]) -> None:
        keys = keys - self.unselectable_keys
        if self.explorer_selection:
            self.show_cursor = self._key_at(self.cursor_row) in keys
        if keys == self.checked_keys:
            return
        changed = self.checked_keys ^ keys
        self.checked_keys = set(keys)
        if "selected" in self.columns:
            for key in changed & set(self.visible_keys()):
                self.update_cell(key, "selected", self.check_label(key))
        self._clear_caches()
        self.refresh()
        self.post_message(self.SelectionChanged(self))

    def _get_row_style(self, row_index: int, base_style: Style) -> Style:
        style = super()._get_row_style(row_index, base_style)
        if self._key_at(row_index) in self.checked_keys:
            style += self.get_component_styles("selectiontable--checked").rich_style
        return style

    def _key_at(self, row: int) -> str | None:
        rows = self.ordered_rows
        return str(rows[row].key.value) if 0 <= row < len(rows) else None

    def _check_range(self, key: str) -> None:
        keys = self.visible_keys()
        if self._anchor not in keys:
            self._anchor = key
            self._range_base = None
        if self._range_base is None:
            self._range_base = set() if self.explorer_selection else set(self.checked_keys)
        start, end = sorted((keys.index(self._anchor), keys.index(key)))
        self._set_checks(self._range_base | set(keys[start : end + 1]))

    def action_toggle_check(self) -> None:
        key = self._key_at(self.cursor_row)
        if key is not None:
            self._anchor = key
            self.set_checks(self.checked_keys ^ {key})

    def action_toggle_all(self) -> None:
        visible = set(self.visible_keys()) - self.unselectable_keys
        if self.explorer_selection:
            self.set_checks(visible)
            return
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
        self.show_cursor = True
        if self._range_base is None:
            self._anchor = self._key_at(self.cursor_row)
        self.move_cursor(row=max(0, min(self.row_count - 1, self.cursor_row + direction)))
        key = self._key_at(self.cursor_row)
        if key is not None:
            self._check_range(key)

    def action_cursor_up(self) -> None:
        self._range_base = None
        if self.explorer_selection:
            self.show_cursor = True
        super().action_cursor_up()
        if self.explorer_selection:
            self._select_current_row()

    def action_cursor_down(self) -> None:
        self._range_base = None
        if self.explorer_selection:
            self.show_cursor = True
        super().action_cursor_down()
        if self.explorer_selection:
            self._select_current_row()

    def action_page_up(self) -> None:
        self._range_base = None
        if self.explorer_selection:
            self.show_cursor = True
        super().action_page_up()
        if self.explorer_selection:
            self._select_current_row()

    def action_page_down(self) -> None:
        self._range_base = None
        if self.explorer_selection:
            self.show_cursor = True
        super().action_page_down()
        if self.explorer_selection:
            self._select_current_row()

    def action_focus_row(self, direction: int) -> None:
        self._range_base = None
        self.move_cursor(row=max(0, min(self.row_count - 1, self.cursor_row + direction)))
        self.restore_selection()

    def action_select_cursor(self) -> None:
        if self.explorer_selection:
            if self._key_at(self.cursor_row) not in self.checked_keys:
                self._select_current_row()
            self.show_cursor = True
        super().action_select_cursor()
        self.restore_selection()

    def _explorer_click(self, event: events.Click, row: int | None) -> None:
        key = self._key_at(row) if row is not None else None
        if event.button == 3:
            if key is not None:
                self.move_cursor(row=row)
                if key not in self.checked_keys:
                    self._select_current_row()
            else:
                self.action_clear_checks()
            self.action_context_menu()
        elif key is None:
            if row == -1:
                return
            if not event.ctrl and not event.shift:
                self.action_clear_checks()
        else:
            if event.shift and self._anchor is None:
                self._anchor = self._key_at(self.cursor_row)
            self.move_cursor(row=row)
            if event.shift:
                self._check_range(key)
            elif event.ctrl:
                self.action_toggle_check()
            else:
                self._select_current_row()
                if event.chain == 2:
                    self.action_select_cursor()
        self.restore_selection()
        self.focus()
        event.stop()
        event.prevent_default()

    def on_click(self, event: events.Click) -> None:
        meta = event.style.meta
        row, column = meta.get("row"), meta.get("column")
        if self.explorer_selection and event.button in (1, 3):
            self._explorer_click(event, row if isinstance(row, int) else None)
            return
        if event.button == 3:
            if isinstance(row, int) and row >= 0:
                self.move_cursor(row=row)
            self.focus()
            self.action_context_menu()
            event.stop()
            event.prevent_default()
            return
        if event.button != 1 or not isinstance(row, int):
            return
        checkbox = column == 0 and "selected" in self.columns
        if row == -1 and checkbox:
            self.action_toggle_all()
        elif row >= 0 and (checkbox or event.ctrl or event.shift or self.selection_mode):
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
            if row >= 0:
                self._anchor = self._key_at(row)
                self._range_base = None
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
