"""Unit tests for JSON localization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mailflow.i18n import I18n


class TestI18nBuiltin:
    def test_english_default(self) -> None:
        i18n = I18n()
        assert i18n.language == "en"
        assert i18n.t("mail.list_title", count=3) == "Mails (3)"

    def test_zh_cn_switch(self) -> None:
        i18n = I18n(language="zh-CN")
        assert i18n.t("mail.list_title", count=3) == "邮件（3）"

    def test_unknown_language_falls_back_to_english(self) -> None:
        i18n = I18n(language="xx")  # not loaded -> falls back to en
        assert i18n.language == "en"

    def test_missing_key_returns_key(self) -> None:
        i18n = I18n()
        assert i18n.t("no.such.key") == "no.such.key"

    def test_format_parameters(self) -> None:
        i18n = I18n()
        assert i18n.t("mail.not_found", mail_id="m1") == "Mail m1 not found"

    def test_available_languages_include_builtins(self) -> None:
        i18n = I18n()
        codes = i18n.available_codes()
        assert "en" in codes
        assert "zh-CN" in codes
        names = {info.code: info.name for info in i18n.available_languages()}
        assert names["en"] == "English"
        assert names["zh-CN"] == "简体中文"

    def test_builtin_pack_key_parity(self) -> None:
        """zh-CN must not silently miss keys that English defines."""
        en = I18n(language="en")
        zh = I18n(language="zh-CN")
        missing = en.keys("en") - zh.keys("zh-CN")
        assert not missing, f"zh-CN missing keys: {sorted(missing)}"
        assert en.key_count("en") == zh.key_count("zh-CN")
        assert en.key_count("en") > 100


class TestI18nExternal:
    def _write_pack(self, tmp_path: Path, code: str, messages: dict[str, Any]) -> None:
        path = tmp_path / f"{code}.json"
        path.write_text(
            json.dumps({"locale": code, "name": code, "messages": messages}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_external_pack_loads(self, tmp_path: Path) -> None:
        self._write_pack(tmp_path, "ja", {"mail": {"list_title": "メール（{count}）"}})
        i18n = I18n(language="en", extra_dirs=[str(tmp_path)])
        assert "ja" in i18n.available_codes()
        i18n.set_language("ja")
        assert i18n.t("mail.list_title", count=2) == "メール（2）"

    def test_missing_key_falls_back_to_english(self, tmp_path: Path) -> None:
        self._write_pack(tmp_path, "ja", {"mail": {"list_title": "メール（{count}）"}})
        i18n = I18n(language="en", extra_dirs=[str(tmp_path)])
        i18n.set_language("ja")
        # key defined only in en -> english text
        assert i18n.t("plugin.title", count=1) == "Plugins (1)"
        # key defined only in ja -> japanese
        assert i18n.t("mail.list_title", count=1) == "メール（1）"

    def test_missing_directory_warns_not_fails(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        i18n = I18n(extra_dirs=[str(missing)])
        assert i18n.language == "en"

    def test_bad_pack_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
        i18n = I18n(extra_dirs=[str(tmp_path)])
        assert "broken" not in i18n.available_codes()
