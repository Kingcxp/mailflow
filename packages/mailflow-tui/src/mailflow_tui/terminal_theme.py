"""Best-effort detection of the terminal's configured background tone."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_WINDOWS_BUILTIN_BACKGROUNDS = {
    "campbell": "#0c0c0c",
    "campbellpowershell": "#012456",
    "onehalfdark": "#282c34",
    "onehalflight": "#fafafa",
    "solariseddark": "#002b36",
    "solarizeddark": "#002b36",
    "solarizedlight": "#fdf6e3",
    "tangodark": "#000000",
    "tangolight": "#ffffff",
    "vintage": "#000000",
}


def _color_mode(value: str) -> str | None:
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", value.strip())
    if match is None:
        return None
    raw = match.group(1)
    channels = (int(raw[index : index + 2], 16) / 255 for index in (0, 2, 4))
    linear = tuple(
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    )
    luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    return "light" if luminance >= 0.18 else "dark"


def _xterm_color(index: int) -> str | None:
    standard = (
        "000000", "cd0000", "00cd00", "cdcd00", "0000ee", "cd00cd", "00cdcd", "e5e5e5",
        "7f7f7f", "ff0000", "00ff00", "ffff00", "5c5cff", "ff00ff", "00ffff", "ffffff",
    )
    if 0 <= index < 16:
        return standard[index]
    if 16 <= index <= 231:
        level = (0, 95, 135, 175, 215, 255)
        value = index - 16
        return "".join(f"{level[part]:02x}" for part in (value // 36, value // 6 % 6, value % 6))
    if 232 <= index <= 255:
        gray = 8 + (index - 232) * 10
        return f"{gray:02x}" * 3
    return None


def _colorfgbg_mode(value: str) -> str | None:
    try:
        background = int(value.rsplit(";", 1)[-1])
    except ValueError:
        return None
    color = _xterm_color(background)
    return _color_mode(color) if color else None


def _remove_jsonc_comments(text: str) -> str:
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
        elif char == '"':
            in_string = True
            output.append(char)
            index += 1
        elif text.startswith("//", index):
            end = text.find("\n", index)
            if end < 0:
                break
            output.append("\n")
            index = end + 1
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                break
            output.extend("\n" for char in text[index : end + 2] if char == "\n")
            index = end + 2
        else:
            output.append(char)
            index += 1
    without_comments = "".join(output)
    return re.sub(r",\s*([}\]])", r"\1", without_comments)


def _windows_terminal_mode() -> str | None:
    profile_id = os.environ.get("WT_PROFILE_ID", "").strip("{} ").casefold()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not profile_id or not local_app_data:
        return None
    base = Path(local_app_data)
    candidates = (
        base / "Packages/Microsoft.WindowsTerminal_8wekyb3d8bbwe/LocalState/settings.json",
        base / "Packages/Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe/LocalState/settings.json",
        base / "Microsoft/Windows Terminal/settings.json",
        base / "Microsoft/Windows Terminal Preview/settings.json",
    )
    for path in candidates:
        try:
            raw = path.read_text(encoding="utf-8-sig")
            try:
                settings: Any = json.loads(raw)
            except ValueError:
                settings = json.loads(_remove_jsonc_comments(raw))
        except (OSError, ValueError):
            continue
        profiles = settings.get("profiles", {}) if isinstance(settings, dict) else {}
        profile_list = profiles.get("list", []) if isinstance(profiles, dict) else []
        profile = next(
            (
                item
                for item in profile_list
                if isinstance(item, dict)
                and str(item.get("guid", "")).strip("{} ").casefold() == profile_id
            ),
            {},
        )
        defaults = profiles.get("defaults", {}) if isinstance(profiles, dict) else {}
        scheme_name = profile.get("colorScheme") if isinstance(profile, dict) else None
        if not scheme_name and isinstance(defaults, dict):
            scheme_name = defaults.get("colorScheme")
        if not scheme_name:
            return None
        normalized_name = re.sub(r"[^a-z0-9]", "", str(scheme_name).casefold())
        for scheme in settings.get("schemes", []):
            if (
                isinstance(scheme, dict)
                and re.sub(r"[^a-z0-9]", "", str(scheme.get("name", "")).casefold())
                == normalized_name
            ):
                return _color_mode(str(scheme.get("background", "")))
        return _color_mode(_WINDOWS_BUILTIN_BACKGROUNDS.get(normalized_name, ""))
    return None


def terminal_background_mode() -> str | None:
    explicit = os.environ.get("MAILFLOW_TERMINAL_BACKGROUND", "").strip().lower()
    if explicit in {"dark", "light"}:
        return explicit
    mode = _windows_terminal_mode()
    if mode is not None:
        return mode
    colorfgbg = os.environ.get("COLORFGBG", "")
    return _colorfgbg_mode(colorfgbg) if colorfgbg else None
