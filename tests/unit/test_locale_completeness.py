"""Every t()-key referenced in host source must exist in BOTH language
packs — a missing key renders as the raw key string, which is exactly the
'classic' silent i18n failure this guard exists to prevent."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from string import Formatter
from typing import Any, cast

_ROOT = Path(__file__).resolve().parents[2]
_LOCALES = _ROOT / "packages" / "mailflow-core" / "src" / "mailflow" / "locale"
_TUI = _ROOT / "packages" / "mailflow-tui" / "src" / "mailflow_tui"
_CLI = _ROOT / "packages" / "mailflow-cli" / "src" / "mailflow_cli"
_CORE_COMMANDS = _ROOT / "packages" / "mailflow-core" / "src" / "mailflow" / "commands.py"

_RUNTIME = _ROOT / "packages" / "mailflow-core" / "src" / "mailflow" / "runtime.py"


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
    for root in (_TUI, _CLI, _CORE_COMMANDS, _RUNTIME):
        for path in [root] if root.is_file() else root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                name = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else node.func.id
                    if isinstance(node.func, ast.Name)
                    else ""
                )
                key = node.args[0]
                if (
                    name in {"t", "_t", "_rt"}
                    and isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                ):
                    keys.add(key.value)
    return keys


def test_every_referenced_key_exists_in_both_packs() -> None:
    referenced = _referenced_keys()
    assert referenced, "key scan found nothing"
    zh = _locale_keys("zh-CN")
    en = _locale_keys("en")
    missing_zh = sorted(k for k in referenced if k not in zh)
    missing_en = sorted(k for k in referenced if k not in en)
    assert not missing_zh, f"keys missing from zh-CN: {missing_zh}"
    assert not missing_en, f"keys missing from en: {missing_en}"


def test_packs_have_parity() -> None:
    assert _locale_keys("zh-CN") == _locale_keys("en")


def test_form_and_gateway_states_render_with_matching_parameters() -> None:
    from mailflow.i18n import I18n

    english, chinese = I18n("en"), I18n("zh-CN")
    keys = {
        key
        for key in english.keys("en")
        if key.startswith(("tui.form_", "tui.gateway_phase_", "tui.remote_", "ux.", "reply.state_"))
    }
    assert keys
    for key in keys:
        en, zh = english.t(key), chinese.t(key)
        en_params = {name for _text, name, _spec, _conversion in Formatter().parse(en) if name}
        zh_params = {name for _text, name, _spec, _conversion in Formatter().parse(zh) if name}
        assert en_params == zh_params, key
        values = dict.fromkeys(en_params, "sample")
        for i18n in (english, chinese):
            rendered = i18n.t(key, **values)
            assert rendered and rendered != key
            assert all(f"{{{name}}}" not in rendered for name in en_params), key
