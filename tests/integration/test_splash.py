"""Themed welcome, immediate skip, and first-launch theme choice."""

from __future__ import annotations

from pathlib import Path

import pytest
from mailflow.themes import DEFAULT_THEME
from mailflow_tui.splash import SplashScreen
from mailflow_tui.theme_preferences import load_theme_preferences, save_theme_preferences
from mailflow_tui.theme_welcome import (
    VSCODE_DARK_THEME,
    VSCODE_LIGHT_THEME,
    ThemeWelcomeScreen,
)
from textual.color import Color
from textual.screen import Screen
from textual.widgets import Static

from mailflow_test_support.app import make_app
from mailflow_test_support.ui import until


@pytest.mark.asyncio
async def test_saved_local_config_theme_wins_over_stale_machine_preference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAILFLOW_SESSION_DIR", str(tmp_path / "sessions"))
    save_theme_preferences(VSCODE_DARK_THEME, completed=True)
    app, service = await make_app(
        tmp_path, theme_welcome=True, config_path=tmp_path / "config.toml"
    )
    try:
        assert app.theme == DEFAULT_THEME
        async with app.run_test() as pilot:
            await until(pilot, lambda: app.theme == DEFAULT_THEME)
            assert app.current_theme.background == "#24273a"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_splash_shows_then_dismisses(tmp_path: Path) -> None:
    """With splash=True the boot screen appears and removes itself."""
    app, service = await make_app(tmp_path, splash=True)
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert isinstance(app.screen, SplashScreen)
            artwork = app.screen.query_one("#splash-art", Static).render()
            assert str(app.screen.query_one("#splash-art", Static).content).isascii()
            await pilot.pause(0.15)
            assert app.screen.query_one("#splash-art", Static).render() != artwork
            # Wait past the automatic welcome (default 3 seconds).
            await pilot.pause(4.0)
            assert not isinstance(app.screen, SplashScreen)
            from textual.widgets import TabbedContent

            assert app.query_one(TabbedContent) is not None
            app.show_startup_splash()
            await pilot.pause()
            assert not isinstance(app.screen, SplashScreen)
    finally:
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gesture", "theme", "size"),
    [
        ("f1", DEFAULT_THEME, (80, 24)),
        ("a", "catppuccin-latte", (60, 24)),
        ("click", "dracula", (120, 36)),
    ],
)
async def test_splash_skip_and_theme(
    tmp_path: Path, gesture: str, theme: str, size: tuple[int, int]
) -> None:
    """Skipping consumes the input and cannot later close another screen."""
    app, service = await make_app(tmp_path)
    app.preview_color_scheme(theme)
    try:
        async with app.run_test(size=size) as pilot:
            splash = SplashScreen(service.t, duration=2.0)
            await app.push_screen(splash)
            await pilot.pause(0.05)
            assert splash.styles.background == Color.parse(app.current_theme.background)
            for node in splash.query(Static):
                assert node.region.right <= size[0]
                assert node.region.bottom <= size[1]
            await pilot.resize_terminal(60, 20)
            await until(
                pilot,
                lambda: (
                    splash.query_one("#splash-skip").region.right <= 60
                    and splash.query_one("#splash-skip").region.bottom <= 20
                ),
            )
            assert splash.query_one("#splash-skip").region.bottom <= 20
            if gesture == "click":
                await pilot.click("#splash-art")
            else:
                await pilot.press(gesture)
            await until(pilot, lambda: not isinstance(app.screen, SplashScreen))
            mail_search = app.query_one("#mail-search")
            assert not mail_search.text
            next_screen = Screen[None]()
            await app.push_screen(next_screen)
            await pilot.pause(2.1)
            assert app.screen is next_screen
            assert not isinstance(app.screen, SplashScreen)
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_splash_off_by_default(tmp_path: Path) -> None:
    """Default construction (tests) does not push the splash screen."""
    app, service = await make_app(tmp_path)
    app.animation_level = "none"
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert not isinstance(app.screen, SplashScreen)
            await app.push_screen(SplashScreen(service.t))
            art = app.screen.query_one("#splash-art", Static)
            artwork = art.render()
            await pilot.pause(0.1)
            assert art.render() == artwork
            await until(pilot, lambda: not isinstance(app.screen, SplashScreen))
    finally:
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm", [False, True], ids=["cancel", "confirm"])
async def test_first_use_theme_choice_is_saved_for_this_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, confirm: bool
) -> None:
    monkeypatch.setenv("MAILFLOW_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("MAILFLOW_TERMINAL_BACKGROUND", "dark")
    app, service = await make_app(tmp_path, theme_welcome=True)
    try:
        async with app.run_test() as pilot:
            await until(pilot, lambda: isinstance(app.screen, ThemeWelcomeScreen))
            assert isinstance(app.screen, ThemeWelcomeScreen)
            await until(pilot, lambda: app.focused is app.screen.query_one("#theme-dark"))
            await until(pilot, lambda: app.theme == VSCODE_DARK_THEME)
            await pilot.hover("#theme-light-row .theme-choice-title")
            await until(pilot, lambda: app.theme == VSCODE_LIGHT_THEME)
            assert app.current_theme.background == "#ffffff"
            assert service.config.general.theme == DEFAULT_THEME
            assert load_theme_preferences() == {}
            await pilot.hover("#theme-current")
            await until(pilot, lambda: app.theme == DEFAULT_THEME)
            # Hovering does not move keyboard focus from the first button.
            await pilot.press("tab")
            await until(pilot, lambda: app.theme == VSCODE_LIGHT_THEME)
            await pilot.press("enter" if confirm else "escape")
            await until(pilot, lambda: not isinstance(app.screen, ThemeWelcomeScreen))
            await until(pilot, lambda: bool(load_theme_preferences()))
            assert app.theme == (VSCODE_LIGHT_THEME if confirm else DEFAULT_THEME)
            assert load_theme_preferences() == (
                {"theme_onboarding_complete": True, "theme": VSCODE_LIGHT_THEME}
                if confirm
                else {"theme_onboarding_complete": True}
            )
    finally:
        await service.stop()
