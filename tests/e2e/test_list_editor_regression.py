"""Reproduce the napcat notifier-form ListEditor bug: the 'admins' list
field must render a usable input + add/delete buttons. The old
implementation rebuilt item rows with the ``with Horizontal(...):`` compose
context manager from a button handler — that manager reads
``app._compose_stacks`` which only exists during compose, so adding or
deleting an item crashed with IndexError and the field was unusable."""

from __future__ import annotations

import queue as queue_module
from pathlib import Path
from typing import Any

import pytest
from mailflow.commands import CommandRouter
from mailflow_tui.app import MailFlowApp
from mailflow_tui.list_editor import ListEditor
from test_settings_forms import start_service_quiet
from textual.widgets import Button, Input


async def _wait_until(pilot: Any, predicate: Any, budget: float = 4.0) -> None:
    """Poll a predicate across pilot pauses (pauses are not sync points on
    slow runners). Mirrors the helper in test_settings_forms."""
    import time as _time

    deadline = _time.monotonic() + budget
    while _time.monotonic() < deadline:
        if predicate():
            await pilot.pause()
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not met in time")


@pytest.mark.asyncio
async def test_napcat_admins_list_editor_is_usable(tmp_path: Path) -> None:
    from mailflow_tui.settings import EntryFormScreen

    service = await start_service_quiet(tmp_path)
    CommandRouter(service)
    app = MailFlowApp(service, queue_module.Queue())
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app.push_screen(EntryFormScreen(service, "notifiers"))
            await pilot.pause(0.2)

            # switch provider to napcat (auto-deploy gateway)
            provider: Any = app.screen.query_one("#field-provider")
            provider.value = "napcat"
            await _wait_until(pilot, lambda: bool(app.screen.query("#extra-admins")))

            editor: ListEditor = app.screen.query_one("#extra-admins", ListEditor)
            assert editor is not None
            # input row renders
            input_box: Input = editor.query_one("#list-editor-input", Input)
            add_btn: Button = editor.query_one("#list-editor-add", Button)
            assert input_box is not None and add_btn is not None
            # add a list item
            input_box.value = "10001"
            add_btn.press()
            await pilot.pause(0.2)
            assert editor.value() == ["10001"]
            # delete it
            del_btn: Button = editor.query_one("#list-editor-del-0", Button)
            del_btn.press()
            await pilot.pause(0.2)
            assert editor.value() == []
    finally:
        await service.stop()
