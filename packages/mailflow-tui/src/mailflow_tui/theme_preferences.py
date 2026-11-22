"""Small per-user state for the first-launch theme choice."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any, cast


def _preferences_path() -> Path:
    directory = Path(
        os.environ.get("MAILFLOW_SESSION_DIR", str(Path.home() / ".mailflow/tui-instances"))
    ).expanduser()
    return directory / "theme-preferences.state"


def load_theme_preferences() -> dict[str, Any]:
    try:
        data = json.loads(_preferences_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


def save_theme_preferences(theme: str | None, *, completed: bool = True) -> bool:
    path = _preferences_path()
    data = load_theme_preferences()
    data["theme_onboarding_complete"] = completed
    if theme is None:
        data.pop("theme", None)
    else:
        data["theme"] = theme
    temporary = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary.write_text(json.dumps(data), encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(path)
        return True
    except OSError:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        return False
