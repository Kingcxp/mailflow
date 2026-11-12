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
from textual.widgets import Button, Input

from mailflow_test_support.settings import start_service_quiet
from mailflow_test_support.ui import until


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
            await until(
                pilot,
                lambda: (
                    app.screen.query_one("#entry-form-next", Button).display
                    and bool(app.screen.query("#extra-admins Input"))
                ),
            )

            editor: ListEditor = app.screen.query_one("#extra-admins", ListEditor)
            # starts with one empty input row
            row0: Input = editor.query_one(Input)
            assert row0.value == ""
            # typing IS the value; no add button needed
            row0.value = "10001"
            assert editor.value() == ["10001"]
            # non-focused rows render the value centered (Textual Input is
            # left-aligned by default; CenteredInput centers while blurred)
            row0.blur()  # pyright: ignore[reportUnknownMemberType]
            await until(pilot, lambda: row0.render_line(0).text.strip() == "10001")
            rendered = row0.render_line(0).text
            assert rendered.strip() == "10001"
            assert rendered.find("10001") > 0, f"value not centered: {rendered!r}"
            # Enter appends a stable-token row, preserves the value and moves focus.
            row0.focus()  # pyright: ignore[reportUnknownMemberType]
            await until(pilot, lambda: row0.has_focus)
            await pilot.press("enter")
            # the new row's focus lands a frame later on slow runners: poll
            await until(pilot, lambda: editor.value() == ["10001"])
            await until(
                pilot,
                lambda: len(editor.query(Input)) == 2,
            )
            row1 = editor.query(Input)[1]
            await until(pilot, lambda: row1.has_focus)
            assert row1.has_focus
            # bottom add appends another row and focuses it without rebuilding prior rows
            add_btn: Button = editor.query_one("#list-editor-add", Button)
            add_btn.press()
            await until(
                pilot,
                lambda: len(editor.query(Input)) == 3,
            )
            row2 = editor.query(Input)[2]
            await until(pilot, lambda: row2.has_focus)
            assert row2.has_focus
            # Delete the original row by its stable token, then focus the next surviving row.
            token = (row0.id or "").removeprefix("list-editor-input-")
            del0: Button = editor.query_one(f"#list-editor-del-{token}", Button)
            del0.press()
            await until(pilot, lambda: editor.value() == [])
            await until(
                pilot,
                lambda: len(editor.query(Input)) == 2,
            )
            await until(pilot, lambda: row1.has_focus)
            assert row1.has_focus

            # collection passes required with a typed value, and the value
            # lands in options.admins (the napcat gateway field)

            form = cast(Any, app.screen)
            # the form requires a notifier id; fill it so collection reaches
            # the admins extra (otherwise the core validation fails first)
            id_field: Input = form.query_one("#field-notifier-id", Input)
            id_field.value = "bot-1"
            row_a: Input = editor.query(Input).first()
            row_a.value = "123456"
            collected = form._collect()
            assert collected["options"]["admins"] == ["123456"]
            # An empty administrator list leaves all mutation commands denied.
            row_a.value = ""
            assert form._collect()["options"].get("admins", []) == []
    finally:
        await service.stop()
