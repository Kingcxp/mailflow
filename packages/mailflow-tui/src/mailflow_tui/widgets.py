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

from typing import Any, cast

from textual.css.query import QueryError
from textual.widgets import Select
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


__all__ = ["SafeSelect"]
