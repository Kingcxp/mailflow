"""Preserve a table's reading position while replacing its data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from textual.coordinate import Coordinate
from textual.widgets import DataTable
from textual.widgets._data_table import RowKey


class TableRowEvent(Protocol):
    data_table: DataTable[Any]
    row_key: RowKey
    cursor_row: int


@dataclass
class TableView:
    row_key: str | None
    row: int
    x: float
    y: float

    @classmethod
    def capture(cls, table: DataTable[Any], row_key: str | None = None) -> TableView:
        row = table.cursor_row
        if row_key is None and 0 <= row < table.row_count:
            row_key = str(table.coordinate_to_cell_key(Coordinate(row, 0)).row_key.value)
        return cls(row_key, row, table.scroll_x, table.scroll_y)

    def restore(self, table: DataTable[Any]) -> str | None:
        keys = [str(key.value) for key in table.rows]
        if not keys:
            return None
        key = self.row_key if self.row_key in keys else keys[max(0, min(self.row, len(keys) - 1))]
        row = table.get_row_index(key)
        table.move_cursor(row=row, scroll=False)

        def restore_scroll() -> None:
            # A queued layout callback must not override a newer selection.
            if table.cursor_row == row:
                table.scroll_to(x=self.x, y=self.y, animate=False, force=True)

        restore_scroll()
        table.call_after_refresh(restore_scroll)
        return key
