"""Splash screen tests: shown at boot when enabled, auto-dismisses, Escape
skips, and default (off) construction never shows it — so existing headless
tests keep running against the main screen directly."""

from __future__ import annotations

from pathlib import Path

import pytest
from mailflow_tui.splash import SplashScreen
from mailflow_tui.theme_preferences import load_theme_preferences
from mailflow_tui.theme_welcome import ThemeWelcomeScreen

from mailflow_test_support.app import make_app


@pytest.mark.asyncio
async def test_splash_shows_then_dismisses(tmp_path: Path) -> None:
    """With splash=True the boot screen appears and removes itself."""
    app, service = await make_app(tmp_path, splash=True)
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert isinstance(app.screen, SplashScreen)
            # wait past the auto-dismiss duration (default 3.5s)
            await pilot.pause(4.0)
            assert not isinstance(app.screen, SplashScreen)
            from textual.widgets import TabbedContent

            assert app.query_one(TabbedContent) is not None
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_splash_escape_skips(tmp_path: Path) -> None:
    """Escape closes the splash before the duration elapses."""
    app, service = await make_app(tmp_path, splash=True)
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
    app, service = await make_app(tmp_path)
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert not isinstance(app.screen, SplashScreen)
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_first_use_theme_choice_is_saved_for_this_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAILFLOW_SESSION_DIR", str(tmp_path / "sessions"))
    app, service = await make_app(tmp_path)
    app._theme_welcome = True
    app._theme_preference = {}
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert isinstance(app.screen, ThemeWelcomeScreen)
            await pilot.click("#theme-light")
            await pilot.pause(0.2)
            assert app.theme == "catppuccin-latte"
            assert load_theme_preferences() == {
                "theme_onboarding_complete": True,
                "theme": "catppuccin-latte",
            }
    finally:
        await service.stop()
