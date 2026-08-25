"""Every t()-key referenced in host source must exist in BOTH language
packs — a missing key renders as the raw key string, which is exactly the
'classic' silent i18n failure this guard exists to prevent."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

_ROOT = Path(__file__).resolve().parents[2]
_LOCALES = _ROOT / "packages" / "mailflow-core" / "src" / "mailflow" / "locale"
_TUI = _ROOT / "packages" / "mailflow-tui" / "src" / "mailflow_tui"
_CLI = _ROOT / "packages" / "mailflow-cli" / "src" / "mailflow_cli"
_CORE_COMMANDS = _ROOT / "packages" / "mailflow-core" / "src" / "mailflow" / "commands.py"

_KEY = re.compile(r"""(?:\b_t|\bservice\.t|\bself\.t)\(\s*["']([a-z][\w.]+)["']""")
_USED = re.compile(r"""(?:\b_t|\bservice\.t|\bself\.t)\(\s*f?["']([a-z][\w.]+)["']""")


def _locale_keys(code: str) -> set[str]:
    locale = _LOCALES / f"{code}.json"
    payload = json.loads(locale.read_text(encoding="utf-8"))
    flattened: set[str] = set()

    def walk(prefix: str, node: dict[str, Any]) -> None:
        for key, value in node.items():
            full = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                walk(full, cast(dict[str, Any], value))
            else:
                flattened.add(full)

    walk("", payload["messages"])
    return flattened


def _referenced_keys() -> set[str]:
    keys: set[str] = set()
    for root in (_TUI, _CLI, _CORE_COMMANDS):
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            keys |= set(_KEY.findall(text))
            keys |= set(_USED.findall(text))
    return keys


def test_every_referenced_key_exists_in_both_packs() -> None:
    referenced = _referenced_keys()
    assert referenced, "key scan found nothing — the regex broke"
    zh = _locale_keys("zh-CN")
    en = _locale_keys("en")
    missing_zh = sorted(k for k in referenced if k not in zh)
    missing_en = sorted(k for k in referenced if k not in en)
    assert not missing_zh, f"keys missing from zh-CN: {missing_zh}"
    assert not missing_en, f"keys missing from en: {missing_en}"


def test_packs_have_parity() -> None:
    assert _locale_keys("zh-CN") == _locale_keys("en")
