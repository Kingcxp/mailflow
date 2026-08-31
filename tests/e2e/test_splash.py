"""Splash screen tests: shown at boot when enabled, auto-dismisses, Escape
skips, and default (off) construction never shows it — so existing headless
tests keep running against the main screen directly."""

from __future__ import annotations

import queue as queue_module
from pathlib import Path
from typing import Any

import pytest
from mailflow.plugins import PluginManager
from mailflow.service import start_service
from mailflow_tui.app import MailFlowApp
from mailflow_tui.splash import SplashScreen
from test_tui import TUIPlugin, build_config


async def _make_app(db_path: Path, *, splash: bool = False) -> tuple[Any, Any]:
    from mailflow_notify_console.plugin import plugin as notify_plugin
    from mailflow_storage_sqlite.plugin import plugin as storage_plugin

    manager = PluginManager(build_config(db_path / "unused.db"))
    manager.register(TUIPlugin())
    manager.register(storage_plugin)
    manager.register(notify_plugin)
    service = await start_service(
        build_config(db_path / "db.sqlite"),
        plugin_manager=manager,
        discover_plugins=False,
    )
    app = MailFlowApp(service, queue_module.Queue(), splash=splash)
    return app, service


@pytest.mark.asyncio
async def test_splash_shows_then_dismisses(tmp_path: Path) -> None:
    """With splash=True the boot screen appears and removes itself."""
    app, service = await _make_app(tmp_path, splash=True)
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert isinstance(app.screen, SplashScreen)
            # wait past the auto-dismiss duration
            await pilot.pause(3.2)
            assert not isinstance(app.screen, SplashScreen)
            from textual.widgets import TabbedContent

            assert app.query_one(TabbedContent) is not None
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_splash_escape_skips(tmp_path: Path) -> None:
    """Escape closes the splash before the duration elapses."""
    app, service = await _make_app(tmp_path, splash=True)
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert isinstance(app.screen, SplashScreen)
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, SplashScreen)
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_splash_off_by_default(tmp_path: Path) -> None:
    """Default construction (tests) does not push the splash screen."""
    app, service = await _make_app(tmp_path)
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert not isinstance(app.screen, SplashScreen)
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_splash_logo_animates(tmp_path: Path) -> None:
    """The logo/widgets exist and the interval animates without crashing."""
    app, service = await _make_app(tmp_path, splash=True)
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            screen: Any = app.screen
            logo = screen.query_one("#splash-logo")
            # a couple of animation ticks
            await pilot.pause(0.5)
            assert logo is not None
            assert screen.query_one("#splash-wave") is not None
    finally:
        await service.stop()
