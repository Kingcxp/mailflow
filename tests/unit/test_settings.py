"""Unit tests for the editable settings layer (``mailflow.settings``)."""

from __future__ import annotations

from pathlib import Path

import pytest
from mailflow.config import LLMConfig, MailFlowConfig
from mailflow.settings import (
    SettingsError,
    add_entry,
    apply_value,
    move_entry,
    normalize_llm_chain,
    remove_entry,
    update_entry,
)


class TestApplyValue:
    def test_optional_string_can_be_set_and_cleared(self) -> None:
        config = MailFlowConfig()
        updated = apply_value(config, "logging.console_redirect", "logs/console.log")
        assert updated.logging.console_redirect == "logs/console.log"
        cleared = apply_value(updated, "logging.console_redirect", "")
        assert cleared.logging.console_redirect is None

    def test_string_list_from_lines(self) -> None:
        updated = apply_value(MailFlowConfig(), "plugins.disabled", "a\n\nb\n")
        assert updated.plugins.disabled == ["a", "b"]

    def test_mapping_from_lines_and_json(self) -> None:
        from_lines = apply_value(
            MailFlowConfig(), "logging.logger_levels", "mailflow.runtime = DEBUG"
        )
        assert from_lines.logging.logger_levels == {"mailflow.runtime": "DEBUG"}
        from_json = apply_value(
            MailFlowConfig(), "logging.logger_levels", '{"mailflow.llm": "ERROR"}'
        )
        assert from_json.logging.logger_levels == {"mailflow.llm": "ERROR"}

    def test_domain_validator_error_is_reported(self) -> None:
        with pytest.raises(SettingsError) as info:
            apply_value(MailFlowConfig(), "general.timezone", "Nowhere/Special")
        assert "timezone" in info.value.message

    def test_invalid_mapping_syntax_reports_the_option(self) -> None:
        with pytest.raises(SettingsError) as info:
            apply_value(MailFlowConfig(), "logging.logger_levels", "not a pair")
        assert info.value.option == "logging.logger_levels"

    def test_unknown_option_rejected(self) -> None:
        with pytest.raises(SettingsError):
            apply_value(MailFlowConfig(), "general.ghost", "1")


class TestListEntries:
    def test_add_update_remove(self) -> None:
        config = add_entry(MailFlowConfig(), "accounts", {"account_id": "a1", "provider": "imap"})
        assert [a.account_id for a in config.accounts] == ["a1"]
        config = update_entry(config, "accounts", 0, {"email": "me@example.com"})
        assert config.accounts[0].email == "me@example.com"
        assert config.accounts[0].provider == "imap"  # untouched fields survive
        config = remove_entry(config, "accounts", 0)
        assert config.accounts == []


class TestLLMChain:
    def _three(self) -> MailFlowConfig:
        config = MailFlowConfig()
        for name in ("a", "b", "c"):
            config = add_entry(config, "llms", {"llm_id": name, "model": f"m-{name}"})
        return normalize_llm_chain(config)

    def test_order_defines_default_and_fallbacks(self) -> None:
        config = self._three()
        assert [llm.llm_id for llm in config.llms] == ["a", "b", "c"]
        assert config.llms[0].default is True
        assert config.llms[0].fallback == ["b", "c"]
        assert config.llms[1].fallback == ["c"]
        assert config.llms[2].fallback == []
        assert config.default_llm() is not None
        assert config.default_llm().llm_id == "a"  # pyright: ignore[reportOptionalMemberAccess]
        config = add_entry(config, "llms", {"llm_id": "d", "model": "m-d"})
        assert config.llms[0].fallback == ["b", "c", "d"]
        config = move_entry(config, "llms", 2, -1)
        assert [llm.llm_id for llm in config.llms] == ["a", "c", "b", "d"]
        assert config.llms[0].fallback == ["c", "b", "d"]
        config = move_entry(config, "llms", 1, -1)
        assert config.llms[0].llm_id == "c" and config.llms[0].default
        assert not config.llms[1].default

    def test_removing_an_llm_keeps_references_valid(self) -> None:
        config = self._three()
        config = add_entry(
            config,
            "processors",
            {
                "processor_id": "p",
                "provider": "llm-importance",
                "llm": "b",
                "fallback_llms": ["c"],
            },
        )
        config = remove_entry(config, "llms", 1)  # drop "b"
        assert [llm.llm_id for llm in config.llms] == ["a", "c"]
        # The explicit fallback becomes primary when its predecessor is deleted.
        processor = config.processor("p")
        assert processor is not None and processor.llm == "c"
        assert "b" not in processor.fallback_llms
        # no llm still lists the removed id as a fallback
        assert all("b" not in llm.fallback for llm in config.llms)

    def test_renaming_the_bound_llm_follows_the_processor(self) -> None:
        config = self._three()
        config = add_entry(
            config,
            "processors",
            {
                "processor_id": "p",
                "provider": "llm-importance",
                "llm": "b",
                "fallback_llms": ["c"],
            },
        )

        renamed = update_entry(config, "llms", 1, {"llm_id": "b2"})

        processor = renamed.processor("p")
        assert processor is not None and processor.llm == "b2"
        assert processor.fallback_llms == ["c"]
        assert renamed.llms[0].fallback == ["b2", "c"]
        assert all("b" not in llm.fallback for llm in renamed.llms)


class TestPlaceholderBookkeeping:
    """${VAR} placeholders must follow entries through edits, never land on
    the wrong entry and never resurrect an explicitly replaced value."""

    @staticmethod
    def _config_with_placeholder() -> MailFlowConfig:
        config = MailFlowConfig.model_validate(
            {
                "general": {"timezone": "UTC"},
                "llms": [
                    {"llm_id": "a", "model": "m1"},
                    {"llm_id": "b", "model": "m2"},
                ],
            }
        )
        config.env_placeholders = {("llms", 1, "api_key"): "${SECRET}"}
        return config

    def test_apply_value_drops_the_edited_leaf_placeholder(self) -> None:
        config = self._config_with_placeholder()
        config.env_placeholders[("general", "timezone")] = "${MF_TZ}"
        updated = apply_value(config, "general.timezone", "Europe/Berlin")
        assert ("general", "timezone") not in updated.env_placeholders
        assert updated.env_placeholders == {("llms", 1, "api_key"): "${SECRET}"}

    def test_remove_entry_shifts_following_indices(self) -> None:
        config = self._config_with_placeholder()
        updated = remove_entry(config, "llms", 0)
        assert updated.env_placeholders == {("llms", 0, "api_key"): "${SECRET}"}

    def test_remove_entry_drops_the_removed_entry_placeholders(self) -> None:
        config = self._config_with_placeholder()
        updated = remove_entry(config, "llms", 1)
        assert updated.env_placeholders == {}

    def test_move_entry_follows_the_moved_entry(self) -> None:
        config = self._config_with_placeholder()
        updated = move_entry(config, "llms", 1, -1)
        assert updated.env_placeholders == {("llms", 0, "api_key"): "${SECRET}"}

    def test_update_entry_drops_replaced_field_placeholders(self) -> None:
        config = self._config_with_placeholder()
        updated = update_entry(config, "llms", 1, {"api_key": "literal"})
        assert updated.env_placeholders == {}

    def test_form_resubmission_preserves_unchanged_secrets(self) -> None:
        config = self._config_with_placeholder()
        config.llms[1].api_key = "resolved"
        config.llms[1].options = {"token": "old", "region": "west"}
        config.env_placeholders[("llms", 1, "options", "token")] = "${TOKEN}"
        values = config.llms[1].model_dump()
        values["model"] = "changed"
        updated = update_entry(config, "llms", 1, values)
        assert updated.env_placeholders == config.env_placeholders
        values["options"] = {"token": "new", "region": "east"}
        updated = update_entry(config, "llms", 1, values)
        assert updated.env_placeholders == {("llms", 1, "api_key"): "${SECRET}"}


@pytest.mark.parametrize("api_key", ["private", ""])
def test_provider_change_does_not_carry_credentials_or_options(api_key: str) -> None:
    config = MailFlowConfig(
        llms=[
            LLMConfig(
                llm_id="main",
                model="model",
                api_key=api_key,
                base_url="https://old",
                options={"old": True},
                headers={"Authorization": "private"},
                extra_body={"provider_option": True},
            )
        ]
    )
    config.env_placeholders[("llms", 0, "api_key")] = "${OLD_API_KEY}"
    updated = update_entry(
        config, "llms", 0, {"provider": "google-vertex", "options": {"project": "p"}}
    )
    assert updated.llms[0].options == {"project": "p"}
    assert updated.llms[0].api_key == ""
    assert updated.llms[0].base_url != "https://old"
    assert updated.llms[0].headers == {}
    assert updated.llms[0].extra_body == {}
    assert updated.env_placeholders == {}


@pytest.mark.parametrize("patch", [False, True])
def test_failed_config_replace_keeps_original_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patch: bool,
) -> None:
    from mailflow.config import patch_config_value, write_config

    config_path = tmp_path / "config.toml"
    original = '[general]\nlanguage = "en"\n'
    config_path.write_text(original, encoding="utf-8")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError):
        if patch:
            patch_config_value(config_path, "general.language", "zh-CN")
        else:
            write_config(MailFlowConfig(), config_path)
    assert config_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.iterdir()) == [config_path]


class TestTimeoutMigration:
    """Configs written by earlier versions pin timeouts a slow model cannot meet."""

    def test_legacy_defaults_are_raised(self, tmp_path: Path) -> None:
        from mailflow.config import load_config

        path = tmp_path / "legacy.toml"
        path.write_text(
            '[general]\ntimezone = "UTC"\n'
            '[[llms]]\nllm_id = "a"\nprovider = "openai-compatible"\ntimeout_seconds = 60\n'
            '[[processors]]\nprocessor_id = "llm-importance"\n'
            'provider = "llm-importance"\nllm = "a"\ntimeout_seconds = 30\n',
            encoding="utf-8",
        )

        config = load_config(path)

        assert [llm.timeout_seconds for llm in config.llms] == [120.0]
        assert [p.timeout_seconds for p in config.processors] == [120.0]
        assert config.timeouts_migrated is True

    def test_a_deliberate_value_is_preserved(self, tmp_path: Path) -> None:
        from mailflow.config import load_config

        path = tmp_path / "custom.toml"
        path.write_text(
            '[general]\ntimezone = "UTC"\n'
            '[[llms]]\nllm_id = "a"\nprovider = "openai-compatible"\ntimeout_seconds = 15\n'
            '[[processors]]\nprocessor_id = "rules"\nprovider = "rules"\ntimeout_seconds = 5\n',
            encoding="utf-8",
        )

        config = load_config(path)

        assert [llm.timeout_seconds for llm in config.llms] == [15.0]
        processor = config.processor("rules")
        assert processor is not None and processor.timeout_seconds == 5.0
        assert config.timeouts_migrated is False
