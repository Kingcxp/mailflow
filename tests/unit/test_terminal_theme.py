"""Terminal theme detection uses the active terminal profile when available."""

from __future__ import annotations

from pathlib import Path

import pytest

from mailflow_tui.terminal_theme import terminal_background_mode


def test_windows_terminal_profile_and_colorfgbg_are_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = (
        tmp_path
        / "Packages"
        / "Microsoft.WindowsTerminal_8wekyb3d8bbwe"
        / "LocalState"
        / "settings.json"
    )
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        '''{
          // Active Windows Terminal profile
          "profiles": {"list": [{"guid": "{profile-1}", "colorScheme": "Paper"},]},
          "schemes": [{"name": "Paper", "background": "#fdf6e3"},],
        }''',
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("WT_PROFILE_ID", "{profile-1}")
    monkeypatch.delenv("MAILFLOW_TERMINAL_BACKGROUND", raising=False)
    monkeypatch.setenv("COLORFGBG", "15;0")
    assert terminal_background_mode() == "light"

    monkeypatch.delenv("WT_PROFILE_ID")
    assert terminal_background_mode() == "dark"
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert terminal_background_mode() == "light"
