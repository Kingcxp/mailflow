"""Theme menu with temporary mouse and keyboard previews."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from textual import events
from textual.app import ComposeResult
from textual.widgets import OptionList, Select
from textual.widgets._select import SelectCurrent, SelectOverlay

from mailflow_tui.widgets import SafeSelect


class _ThemeOverlay(SelectOverlay):
    def on_mouse_move(self, event: events.MouseMove) -> None:
        index = event.style.meta.get("option")
        if index is not None and not self.get_option_at_index(index).disabled:
            self.highlighted = index

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        select = self.parent
        if isinstance(select, ThemeSelect) and select.expanded:
            select.preview_option(event.option_index)


class ThemeSelect(SafeSelect):
    def __init__(
        self,
        options: Iterable[tuple[str, str]],
        *,
        on_preview: Callable[[str | None], None],
        value: object,
        id: str,
        classes: str,
    ) -> None:
        self._on_preview = on_preview
        self._previewing = False
        super().__init__(options, value=value, id=id, classes=classes, allow_blank=False)

    def compose(self) -> ComposeResult:
        yield SelectCurrent(self.prompt)
        yield _ThemeOverlay(type_to_search=self._type_to_search).data_bind(compact=Select.compact)

    def preview_option(self, index: int) -> None:
        value = self._options[index][1]
        if isinstance(value, str):
            self._previewing = True
            self._on_preview(value)

    def _restore_theme(self) -> None:
        if self._previewing:
            self._previewing = False
            self._on_preview(None)

    def _watch_expanded(self, expanded: bool) -> None:
        super()._watch_expanded(expanded)
        if not expanded:
            self._restore_theme()

    def on_hide(self) -> None:
        self.expanded = False
        self._restore_theme()

    def on_unmount(self) -> None:
        self._restore_theme()
