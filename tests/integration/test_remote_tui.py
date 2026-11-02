"""Remote TUI localization and language-relay behavior."""

from __future__ import annotations

from typing import Any, cast

import pytest
from mailflow.i18n import I18n
from mailflow_tui.remote import LoginScreen, RemoteServiceAdapter
from textual.app import App
from textual.widgets import Button, Static


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
        {"language": "en", "timezone": "UTC"},
        I18n(),
    )

    assert adapter.t("tui.btn_quit") == I18n("en").t("tui.btn_quit")

    client.emit("language.changed", language="zh-CN")

    assert adapter.t("tui.btn_quit") == I18n("zh-CN").t("tui.btn_quit")
    assert adapter.snapshot_sync()["language"] == "zh-CN"


@pytest.mark.asyncio
async def test_remote_login_uses_saved_language_and_validates_required_fields() -> None:
    """A saved language localizes the disconnected login surface too."""

    app = App[None]()
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(LoginScreen({"language": "zh-CN"}))
        await pilot.pause()

        screen = app.screen
        assert isinstance(screen, LoginScreen)
        assert str(screen.query_one("#login-connect", Button).label) == I18n("zh-CN").t(
            "tui.remote_connect"
        )

        screen.query_one("#login-connect", Button).press()
        await pilot.pause()

        status = str(screen.query_one("#login-status", Static).render())
        assert I18n("zh-CN").t("tui.remote_fields_required") in status
