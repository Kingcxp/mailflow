"""Actions-pane behaviour a refresh must not destroy.

Three properties the pane previously got wrong: a refresh threw the list back
to the top, an entry that had already passed looked exactly like one still
ahead, and the confirmation dialog focused the destructive button.
"""

from __future__ import annotations

import queue as queue_module
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from mailflow.config import MailFlowConfig
from mailflow.service import MailFlowService
from mailflow_bundled import create_plugin_manager
from mailflow_tui.app import ActionsPane, MailFlowApp
from mailflow_tui.confirm import ConfirmModal
from textual.coordinate import Coordinate
from textual.widgets import Button, DataTable, Static


def build_config(db_path: Path) -> MailFlowConfig:
    config = MailFlowConfig()
    config.storage.path = str(db_path)
    return config


async def start_service_quiet(tmp_path: Path) -> MailFlowService:
    from mailflow.service import start_service

    config_path = tmp_path / "cfg.toml"
    config_path.write_text("", encoding="utf-8")
    config = build_config(tmp_path / "tui.db")
    manager = create_plugin_manager(config, discover_external=False)
    return await start_service(
        config,
        config_path=config_path,
        plugin_manager=manager,
        discover_plugins=False,
        enable_logging=False,
    )


async def _wait_until(pilot: Any, predicate: Any, budget: float = 6.0) -> None:
    import time as _time

    deadline = _time.monotonic() + budget
    while _time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition never became true")


async def _seed_actions(service: MailFlowService, count: int) -> None:
    """Custom todos spread over the next weeks, so none is expired."""
    base = datetime.now(UTC) + timedelta(days=1)
    for index in range(count):
        await service.add_action(f"Todo {index:02d}", base + timedelta(hours=index))


async def test_refresh_keeps_the_cursor_row_and_the_scroll_offset(tmp_path: Path) -> None:
    service = await start_service_quiet(tmp_path)
    await _seed_actions(service, 30)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            pane = app.query_one(ActionsPane)
            await pane.refresh_actions()
            table = cast(DataTable[Any], app.query_one("#actions-table", DataTable))
            assert table.row_count == 30

            # put the cursor deep enough that the list has really scrolled
            table.move_cursor(row=25)
            await pilot.pause(0.3)
            before_scroll = int(table.scroll_y)
            assert before_scroll > 0, "the viewport did not scroll at all"
            before_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value

            await pane.refresh_actions()
            await pilot.pause(0.3)

            assert table.row_count == 30
            assert int(table.scroll_y) == before_scroll, "a refresh threw the list back"
            after_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value
            assert after_key == before_key, "the cursor row changed under the user"
    finally:
        await service.stop()


async def test_a_refresh_after_a_delete_does_not_jump_to_the_top(tmp_path: Path) -> None:
    service = await start_service_quiet(tmp_path)
    await _seed_actions(service, 30)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            pane = app.query_one(ActionsPane)
            await pane.refresh_actions()
            table = cast(DataTable[Any], app.query_one("#actions-table", DataTable))

            table.move_cursor(row=25)
            await pilot.pause(0.3)
            before_scroll = int(table.scroll_y)
            items = list(pane._items)  # pyright: ignore[reportPrivateUsage]
            await service.delete_action(items[0].item_id)

            await pane.refresh_actions()
            await pilot.pause(0.3)

            assert table.row_count == 29
            # the removed entry was above the viewport, so the offset survives
            assert int(table.scroll_y) >= before_scroll - 1
    finally:
        await service.stop()


async def test_expired_rows_are_dimmed_and_future_rows_are_not(tmp_path: Path) -> None:
    service = await start_service_quiet(tmp_path)
    # over, but not yet swept: the sweep retires entries a day after they end
    await service.add_action("Just ended", datetime.now(UTC) - timedelta(hours=2))
    await service.add_action("Still ahead", datetime.now(UTC) + timedelta(days=3))
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            pane = app.query_one(ActionsPane)
            await pane.refresh_actions()
            table = cast(DataTable[Any], app.query_one("#actions-table", DataTable))
            assert table.row_count == 2

            styles = {
                str(table.get_row_at(index)[2]): str(table.get_row_at(index)[2].style)
                for index in range(2)
            }
            assert "dim" in styles["Just ended"], styles
            assert "dim" not in styles["Still ahead"], styles
    finally:
        await service.stop()


async def test_confirm_modal_starts_on_cancel(tmp_path: Path) -> None:
    """Enter must never confirm a destructive dialog the user did not read."""
    service = await start_service_quiet(tmp_path)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            results: list[bool] = []
            app.push_screen(
                ConfirmModal(
                    service,
                    title="Delete?",
                    body="This would delete things.",
                    confirm_label="Delete",
                    variant="error",
                ),
                lambda value: results.append(bool(value)),
            )
            await _wait_until(pilot, lambda: isinstance(app.screen, ConfirmModal))
            await _wait_until(pilot, lambda: app.focused is not None)

            focused = app.focused
            assert focused is not None
            assert focused.id == "confirm-cancel", f"focus started on {focused.id}"

            await pilot.press("enter")
            await pilot.pause(0.2)

            assert not isinstance(app.screen, ConfirmModal)
            assert results == [False], "Enter confirmed a destructive dialog"
    finally:
        await service.stop()


async def test_confirm_modal_confirm_button_still_works(tmp_path: Path) -> None:
    service = await start_service_quiet(tmp_path)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            results: list[bool] = []
            app.push_screen(
                ConfirmModal(
                    service,
                    title="Apply?",
                    body="Apply the changes.",
                    confirm_label="Apply",
                    variant="warning",
                ),
                lambda value: results.append(bool(value)),
            )
            await _wait_until(pilot, lambda: isinstance(app.screen, ConfirmModal))
            await _wait_until(pilot, lambda: bool(app.screen.query("#confirm-run")))

            app.screen.query_one("#confirm-run", Button).press()
            await _wait_until(pilot, lambda: bool(results))

            assert results == [True]
    finally:
        await service.stop()


async def test_actions_pane_has_no_seminar_review_control(tmp_path: Path) -> None:
    """Discovery is one smart-action operation, not a standing button."""
    service = await start_service_quiet(tmp_path)
    app = MailFlowApp(cast(Any, service), queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            pane = app.query_one(ActionsPane)
            await pane.refresh_actions()
            assert not pane.query("#actions-review-seminars")
            assert bool(pane.query("#actions-add"))
            hint = pane.query_one("#actions-hint", Static)
            assert hint is not None
    finally:
        await service.stop()
