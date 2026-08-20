"""Unit tests for the editable settings layer (``mailflow.settings``)."""

from __future__ import annotations

import pytest
from mailflow.config import MailFlowConfig
from mailflow.settings import (
    EditorKind,
    SettingsError,
    add_entry,
    apply_value,
    build_sections,
    entry_field_specs,
    entry_model,
    find_spec,
    move_entry,
    normalize_llm_chain,
    remove_entry,
    reset_value,
    update_entry,
)


def config_with_chain() -> MailFlowConfig:
    return MailFlowConfig.model_validate(
        {
            "processors": [
                {"processor_id": "rules", "provider": "rules", "priority": 10},
                {"processor_id": "llm", "provider": "llm-importance", "priority": 20},
            ],
            "notifiers": [{"notifier_id": "console", "provider": "console"}],
        }
    )


class TestSections:
    def test_core_sections_come_first(self) -> None:
        sections = build_sections(MailFlowConfig())
        ids = [section.section_id for section in sections]
        assert ids[:5] == ["general", "logging", "plugins", "storage", "i18n"]

    def test_plugin_options_land_in_their_own_section(self) -> None:
        sections = {s.section_id: s for s in build_sections(config_with_chain())}
        # without a registry the provider id is the fallback section id
        assert "rules" in sections
        assert "llm-importance" in sections
        assert "console" in sections
        keys = {spec.key for spec in sections["rules"].options}
        assert "processors[0].priority" in keys

    def test_accounts_and_llms_are_not_in_the_sidebar(self) -> None:
        """They have dedicated tabs; listing them twice would be a second
        convention for the same data."""
        config = MailFlowConfig.model_validate(
            {
                "accounts": [{"account_id": "a", "provider": "imap"}],
                "llms": [{"llm_id": "l", "provider": "openai-compatible"}],
            }
        )
        keys = {spec.key for section in build_sections(config) for spec in section.options}
        assert not any(key.startswith("accounts[") for key in keys)
        assert not any(key.startswith("llms[") for key in keys)

    def test_editor_kinds_follow_the_schema(self) -> None:
        config = MailFlowConfig()
        assert find_spec(config, "general.workers").editor is EditorKind.INTEGER  # pyright: ignore[reportOptionalMemberAccess]
        assert find_spec(config, "general.auto_update").editor is EditorKind.BOOLEAN  # pyright: ignore[reportOptionalMemberAccess]
        assert find_spec(config, "logging.logger_levels").editor is EditorKind.MAPPING  # pyright: ignore[reportOptionalMemberAccess]
        assert find_spec(config, "plugins.disabled").editor is EditorKind.STRING_LIST  # pyright: ignore[reportOptionalMemberAccess]
        assert find_spec(config, "plugins.repositories").editor is EditorKind.STRUCT_LIST  # pyright: ignore[reportOptionalMemberAccess]

    def test_secret_fields_are_marked(self) -> None:
        specs = {spec.label: spec for spec in entry_field_specs(entry_model("llms"))}
        assert specs["api_key"].editor is EditorKind.SECRET
        assert specs["api_key"].secret is True
        assert specs["model"].secret is False

    def test_urgency_choice_offers_the_four_levels(self) -> None:
        specs = {spec.label: spec for spec in entry_field_specs(entry_model("notifiers"))}
        urgency = specs["minimum_urgency"]
        assert urgency.editor is EditorKind.CHOICE
        assert urgency.choices == ("ad", "info", "important", "urgent")


class TestApplyValue:
    def test_scalar_coercion(self) -> None:
        config = MailFlowConfig()
        assert apply_value(config, "general.workers", "4").general.workers == 4
        assert apply_value(config, "general.auto_update", "off").general.auto_update is False

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

    def test_type_error_names_the_option(self) -> None:
        with pytest.raises(SettingsError) as info:
            apply_value(MailFlowConfig(), "general.workers", "many")
        assert info.value.option == "general.workers"
        assert "whole number" in info.value.message

    def test_range_violation_reports_the_constraint(self) -> None:
        with pytest.raises(SettingsError) as info:
            apply_value(MailFlowConfig(), "general.cleanup_hour", "99")
        assert info.value.option == "general.cleanup_hour"
        assert "23" in info.value.message

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


class TestResetValue:
    def test_reset_restores_the_schema_default(self) -> None:
        changed = apply_value(MailFlowConfig(), "general.workers", "9")
        assert changed.general.workers == 9
        assert reset_value(changed, "general.workers").general.workers == 2

    def test_reset_clears_an_optional_string(self) -> None:
        changed = apply_value(MailFlowConfig(), "logging.console_redirect", "logs/c.log")
        assert reset_value(changed, "logging.console_redirect").logging.console_redirect is None

    def test_is_default_tracks_modification(self) -> None:
        config = MailFlowConfig()
        assert find_spec(config, "general.workers").is_default() is True  # pyright: ignore[reportOptionalMemberAccess]
        changed = apply_value(config, "general.workers", "3")
        assert find_spec(changed, "general.workers").is_default() is False  # pyright: ignore[reportOptionalMemberAccess]


class TestListEntries:
    def test_add_update_remove(self) -> None:
        config = add_entry(MailFlowConfig(), "accounts", {"account_id": "a1", "provider": "imap"})
        assert [a.account_id for a in config.accounts] == ["a1"]
        config = update_entry(config, "accounts", 0, {"email": "me@example.com"})
        assert config.accounts[0].email == "me@example.com"
        assert config.accounts[0].provider == "imap"  # untouched fields survive
        config = remove_entry(config, "accounts", 0)
        assert config.accounts == []

    def test_add_reports_missing_required_field(self) -> None:
        with pytest.raises(SettingsError) as info:
            add_entry(MailFlowConfig(), "accounts", {"provider": "imap"})
        assert "account_id" in info.value.message

    def test_remove_out_of_range(self) -> None:
        with pytest.raises(SettingsError):
            remove_entry(MailFlowConfig(), "accounts", 3)

    def test_unknown_group_rejected(self) -> None:
        with pytest.raises(SettingsError):
            add_entry(MailFlowConfig(), "general", {})


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

    def test_moving_an_entry_rebuilds_the_chain(self) -> None:
        config = move_entry(self._three(), "llms", 2, -1)
        assert [llm.llm_id for llm in config.llms] == ["a", "c", "b"]
        assert config.llms[0].fallback == ["c", "b"]
        assert config.llms[1].fallback == ["b"]

    def test_promoting_to_first_switches_the_default(self) -> None:
        config = move_entry(self._three(), "llms", 1, -1)
        assert config.llms[0].llm_id == "b"
        assert config.llms[0].default is True
        assert config.llms[1].default is False

    def test_move_at_the_edge_is_a_noop(self) -> None:
        config = self._three()
        assert move_entry(config, "llms", 0, -1) is config
        assert move_entry(config, "llms", 2, 1) is config

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
        # the processor referenced the removed llm: cleared, not left dangling
        assert config.processors[0].llm is None
        assert config.processors[0].fallback_llms == []
        # no llm still lists the removed id as a fallback
        assert all("b" not in llm.fallback for llm in config.llms)
