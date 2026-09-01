"""Reproduce the napcat notifier-form ListEditor bug: the 'admins' list
field must render editable rows with per-row delete and a bottom add
button. Typing in a row is the value (no hidden add-to-list step), Enter
appends a row, and a required field validates against what is typed."""

from __future__ import annotations

import queue as queue_module
from pathlib import Path
from typing import Any, cast

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
            # starts with one empty input row
            row0: Input = editor.query_one("#list-editor-input-0", Input)
            assert row0.value == ""
            # typing IS the value; no add button needed
            row0.value = "10001"
            assert editor.value() == ["10001"]
            # Enter appends a new row and keeps the typed value
            row0.focus()  # pyright: ignore[reportUnknownMemberType]
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert editor.value() == ["10001"]
            assert editor.query_one("#list-editor-input-1", Input) is not None
            # bottom add button appends another row
            add_btn: Button = editor.query_one("#list-editor-add", Button)
            add_btn.press()
            await pilot.pause(0.2)
            assert editor.query_one("#list-editor-input-2", Input) is not None
            # per-row delete removes that row's value
            del0: Button = editor.query_one("#list-editor-del-0", Button)
            del0.press()
            await pilot.pause(0.2)
            assert editor.value() == []

            # collection passes required with a typed value, and the value
            # lands in options.admins (the napcat gateway field)
            from mailflow.settings import SettingsError

            form = cast(Any, app.screen)
            # the form requires a notifier id; fill it so collection reaches
            # the admins extra (otherwise the core validation fails first)
            id_field: Input = form.query_one("#field-notifier-id", Input)
            id_field.value = "bot-1"
            row_a: Input = form.query_one("#extra-admins #list-editor-input-0", Input)
            row_a.value = "123456"
            collected = form._collect()
            assert collected["options"]["admins"] == ["123456"]
            # empty admins still fails required validation
            row_a.value = ""
            try:
                form._collect()
                raise AssertionError("expected SettingsError for empty admins")
            except SettingsError as exc:
                assert exc.option == "admins"
                assert "required" in exc.message
    finally:
        await service.stop()
