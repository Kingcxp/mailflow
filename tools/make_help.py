"""Render Make targets without importing Waymark Mail or installing dependencies."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import unicodedata
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent


def cell_width(text: str) -> int:
    return sum(
        0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
        for char in text
    )


def wrap(text: str, width: int) -> list[str]:
    """Wrap English words and Chinese characters by terminal cells."""
    lines: list[str] = []
    while cell_width(text) > width:
        used = 0
        end = 0
        for end, char in enumerate(text, start=1):
            used += cell_width(char)
            if used > width:
                end -= 1
                break
        space = text.rfind(" ", 0, end + 1)
        if space > 0:
            end = space
        lines.append(text[:end].rstrip())
        text = text[end:].lstrip()
    return [*lines, text]


def help_text(language: str, width: int, color: bool) -> str:
    # Reuse the built-in translations as data. Importing mailflow.i18n would
    # first import domain models, which need an installed runtime environment.
    locale = ROOT / "packages/mailflow-core/src/mailflow/locale" / f"{language}.json"
    messages = cast(dict[str, Any], json.loads(locale.read_text("utf-8"))["messages"]["make"])

    def t(key: str, fallback: str = "") -> str:
        value: Any = messages
        for part in key.split("."):
            value = value.get(part, {})
        return str(value) if isinstance(value, str) else fallback

    def styled(value: str, style: str) -> str:
        return f"\033[{style}m{value}\033[0m" if color else value

    lines = [styled(t("title"), "1"), t("usage")]
    sections = {
        "Setup": "setup",
        "Quality gates": "checks",
        "Run": "run",
        "Package": "package",
        "Bot framework plugins": "bots",
        "Cleanup": "cleanup",
        "Help": "help",
    }
    for line in (ROOT / "Makefile").read_text("utf-8").splitlines():
        if line.startswith("### "):
            name = line[4:]
            lines.extend(("", styled(t("sections." + sections.get(name, name), name), "1")))
        elif match := re.match(r"^([\w-]+):.*?## (.+)$", line):
            name, default = match.groups()
            description = wrap(t("targets." + name, default), max(16, width - 24))
            style = "31" if name == "clean-trash" else "33" if name.startswith("clean-") else "36"
            lines.append("  " + styled(name.ljust(20), style) + "  " + description[0])
            lines.extend(" " * 24 + part for part in description[1:])

    lines.extend(("", styled(t("options_title"), "1")))
    for name in ("VENV", "CONFIG", "SESSION", "FRAMEWORK", "OUTPUT", "HELP_LANG"):
        description = wrap(t("options." + name), max(16, width - 24))
        lines.append("  " + styled(name.ljust(20), "36") + "  " + description[0])
        lines.extend(" " * 24 + part for part in description[1:])
    lines.append("")
    lines.extend(wrap(t("environment_note"), width))
    lines.extend(wrap(t("examples"), width))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=("en", "zh-CN"), default="en")
    args = parser.parse_args()
    force_color = os.environ.get("FORCE_COLOR", "0") not in ("", "0")
    color = "NO_COLOR" not in os.environ and (
        force_color or (sys.stdout.isatty() and os.environ.get("TERM") != "dumb")
    )
    print(help_text(args.language, max(40, shutil.get_terminal_size().columns), color))


if __name__ == "__main__":
    main()
