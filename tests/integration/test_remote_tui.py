"""Remote TUI localization and language-relay behavior."""

from __future__ import annotations

import asyncio
import queue
from pathlib import Path
from typing import Any, cast

import pytest
from mailflow.i18n import I18n
from mailflow.mail_query import MailQuery
from mailflow.themes import ButtonColors, ColorScheme
from mailflow_tui.app import MailFlowApp
from mailflow_tui.remote import LoginApp, LoginScreen, RemoteServiceAdapter
from mailflow_tui.widgets import Input
from mailflow_tui.workspace_screens import AssistantHistoryScreen, SavedFiltersScreen
from textual.widgets import Button, Static

from mailflow_test_support.settings import start_service_quiet
from mailflow_test_support.ui import until


class _RemoteClientStub:
    """Enough of the event-client surface for adapter behavior tests."""

    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event: str, **payload: Any) -> None:
        for handler in self.handlers.get(event, []):
            handler(event=event, **payload)


def test_remote_adapter_follows_server_language_event() -> None:
    client = _RemoteClientStub()
    adapter = RemoteServiceAdapter(
        cast(Any, client),
        {
            "language": "en",
            "timezone": "UTC",
            "theme": "custom-latte",
            "color_schemes": {
                "custom-latte": ColorScheme(
                    name="Local light",
                    base="latte",
                    buttons={"primary": ButtonColors(background="#2855ba", foreground="#ffffff")},
                ).model_dump()
            },
        },
        I18n(),
    )

    assert adapter.t("tui.btn_quit") == I18n("en").t("tui.btn_quit")
    assert adapter.config.general.theme == "custom-latte"
    assert not adapter.color_schemes()["custom-latte"].is_dark
    colors = adapter.color_schemes()["custom-latte"].button_palette()["primary"]
    assert (colors["background"], colors["foreground"]) == ("#2855ba", "#ffffff")

    client.emit("language.changed", language="zh-CN")

    assert adapter.t("tui.btn_quit") == I18n("zh-CN").t("tui.btn_quit")
    assert adapter.snapshot_sync()["language"] == "zh-CN"


@pytest.mark.asyncio
async def test_remote_login_uses_saved_language_and_validates_required_fields() -> None:
    """A saved language localizes the disconnected login surface too."""

    app = LoginApp({"language": "zh-CN"})
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()

        screen = app.screen
        assert isinstance(screen, LoginScreen)
        assert screen.query_one("#login-connect", Button).region.bottom <= 24
        assert str(screen.query_one("#login-connect", Button).label) == I18n("zh-CN").t(
            "tui.remote_connect"
        )

        screen.query_one("#login-connect", Button).press()
        await pilot.pause()

        status = str(screen.query_one("#login-status", Static).render())
        assert I18n("zh-CN").t("tui.remote_fields_required") in status


@pytest.mark.asyncio
async def test_slow_dialog_load_and_save_can_be_closed_without_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = await start_service_quiet(tmp_path)
    entered, released = asyncio.Event(), asyncio.Event()

    async def slow_history(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        try:
            await asyncio.Future[None]()
        finally:
            released.set()

    monkeypatch.setattr(service, "workspace_call", slow_history)
    app = MailFlowApp(service, queue.Queue())
    try:
        async with app.run_test(size=(80, 30)) as pilot:
            dialog = AssistantHistoryScreen(service)
            app.push_screen(dialog)
            await until(pilot, entered.is_set)
            await pilot.press("escape")
            await until(pilot, lambda: app.screen is not dialog and released.is_set())

            entered.clear()
            released.clear()

            async def slow_save(operation: str, values: Any = None) -> Any:
                if operation == "filters":
                    return []
                return await slow_history()

            monkeypatch.setattr(service, "workspace_call", slow_save)
            filters = SavedFiltersScreen(service, MailQuery())
            app.push_screen(filters)
            await pilot.pause()
            filters.query_one("#filter-name", Input).value = "My filter"
            filters.query_one("#filter-save", Button).press()
            await until(pilot, entered.is_set)
            assert filters.query_one("#filter-save", Button).disabled
            assert not filters.query_one("#filter-close", Button).disabled
            await pilot.press("escape")
            await until(pilot, lambda: app.screen is not filters and released.is_set())
    finally:
        await service.stop()
