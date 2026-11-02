"""Unit tests for JSON localization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from mailflow.i18n import I18n


def _duplicate_lookup_paths(code: str) -> list[str]:
    """Flattened lookup paths that a built-in pack defines more than once.

    ``I18n`` flattens nested objects with dots, so a literal
    ``"tui.btn_cancel"`` key and the nested ``tui`` → ``btn_cancel`` entry
    resolve to the same path: whichever the flattener visits last silently
    wins, and editing the other one does nothing.
    """
    from importlib import resources

    raw = resources.files("mailflow").joinpath("locale").joinpath(f"{code}.json").read_text("utf-8")
    payload: dict[str, Any] = json.loads(raw)
    messages: dict[str, Any] = payload["messages"]
    counts: dict[str, int] = {}

    def walk(node: dict[str, Any], prefix: str) -> None:
        for key in sorted(node):
            value = node[key]
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                walk(cast(dict[str, Any], value), f"{path}.")
            else:
                counts[path] = counts.get(path, 0) + 1

    walk(messages, "")
    return sorted(path for path, count in counts.items() if count > 1)


class TestI18nBuiltin:
    def test_zh_cn_switch(self) -> None:
        i18n = I18n()
        english = i18n.t("mail.list_title", count=37)
        i18n.set_language("zh-CN")
        rendered = i18n.t("mail.list_title", count=37)
        assert i18n.language == "zh-CN"
        assert "37" in rendered and rendered != english
        assert "unique-mail-37" in i18n.t("mail.not_found", mail_id="unique-mail-37")
        assert "${ENV_VAR}" in i18n.t("config.desc.llms[].api_key")

    def test_no_duplicate_lookup_paths_in_builtin_packs(self) -> None:
        """No key may be reachable by two different spellings.

        A literal ``"tui.btn_cancel"`` sitting next to a ``tui`` object
        flattens to the same lookup path as ``tui`` → ``btn_cancel``: the
        flattener's iteration order decides which value wins, so editing the
        other one silently does nothing. ``config.desc`` is exempt from the
        nesting style — its keys *are* dotted config paths
        (``config.desc.general.workers``) — but it must still not collide.
        """
        for code in ("en", "zh-CN"):
            duplicates = _duplicate_lookup_paths(code)
            assert not duplicates, f"{code}.json defines these twice: {duplicates}"

    def test_every_config_option_has_a_localized_description(self) -> None:
        from mailflow.config import MailFlowConfig
        from mailflow.settings import build_sections, entry_field_specs, entry_model

        config = MailFlowConfig()
        keys = [spec.key for section in build_sections(config) for spec in section.options]
        for group in ("accounts", "llms", "processors", "notifiers"):
            keys += [f"{group}[].{spec.label}" for spec in entry_field_specs(entry_model(group))]

        for code in ("en", "zh-CN"):
            i18n = I18n(language=code)
            missing = [key for key in keys if i18n.t(f"config.desc.{key}") == f"config.desc.{key}"]
            assert not missing, f"{code} lacks config.desc for: {sorted(missing)}"


class TestI18nExternal:
    def _write_pack(self, tmp_path: Path, code: str, messages: dict[str, Any]) -> None:
        path = tmp_path / f"{code}.json"
        path.write_text(
            json.dumps({"locale": code, "name": code, "messages": messages}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_missing_key_falls_back_to_english(self, tmp_path: Path) -> None:
        self._write_pack(tmp_path, "ja", {"mail": {"list_title": "メール（{count}）"}})
        i18n = I18n(language="en", extra_dirs=[str(tmp_path)])
        i18n.set_language("ja")
        # key defined only in en -> english text
        assert i18n.t("plugin.title", count=1) == I18n("en").t("plugin.title", count=1)
        # key defined only in ja -> japanese
        assert i18n.t("mail.list_title", count=1) == "メール（1）"
        assert i18n.t("no.such.key") == "no.such.key"

    def test_bad_pack_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
        i18n = I18n(language="xx", extra_dirs=[str(tmp_path), str(tmp_path / "missing")])
        assert "broken" not in i18n.available_codes()
        assert i18n.language == "en"
