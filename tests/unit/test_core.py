"""Unit tests for the MailFlow core domain contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mailflow.config import (
    LoggingConfig,
    MailFlowConfig,
    load_config,
    patch_config_value,
    set_option_value,
    write_config,
)
from mailflow.domain import (
    MailAnalysis,
    MailMessage,
    MailRecord,
    Urgency,
    parse_urgency,
)
from mailflow.logging import configure_logging

ADDRESS = {
    "name": "Sender",
    "address": "sender@example.com",
}


def make_mail(**overrides: Any) -> MailMessage:
    base: dict[str, Any] = {
        "message_id": "msg-1",
        "account_id": "acct-1",
        "subject": "Hello",
        "sender": ADDRESS,
        "recipients": [],
        "cc": [],
        "date": "2026-01-01T10:00:00+00:00",
        "received_at": "2026-01-01T10:05:00+00:00",
        "body_text": "body",
        "body_html": "<p>body</p>",
        "provider": "fake",
    }
    base.update(overrides)
    return MailMessage.model_validate(base)


class TestUrgencyContract:
    """The four-level urgency contract is public and fixed."""

    def test_parse_synonyms(self) -> None:
        assert parse_urgency("urgent") is Urgency.URGENT
        assert parse_urgency("critical") is Urgency.URGENT
        assert parse_urgency("junk") is Urgency.AD
        assert parse_urgency("ads") is Urgency.AD
        assert parse_urgency("medium") is Urgency.IMPORTANT
        assert parse_urgency("重要") is Urgency.IMPORTANT
        assert parse_urgency("紧急") is Urgency.URGENT
        assert parse_urgency("广告") is Urgency.AD
        assert parse_urgency("信息") is Urgency.INFO
        assert parse_urgency("bogus-value") is Urgency.INFO
        assert parse_urgency(None) is Urgency.INFO


class TestMailRecordUrgency:
    def test_reset_restores_automatic(self) -> None:
        record = MailRecord(
            record_id="r1",
            mail=make_mail(),
            auto_urgency=Urgency.IMPORTANT,
            manual_urgency=Urgency.URGENT,
        )
        assert record.effective_urgency is Urgency.URGENT
        assert record.auto_urgency is Urgency.IMPORTANT
        assert MailRecord.model_validate_json(record.model_dump_json()) == record
        record.manual_urgency = None
        assert record.effective_urgency is Urgency.IMPORTANT

    def test_summary_distinguishes_generated_and_fallback_analysis(self) -> None:
        record = MailRecord(record_id="r1", mail=make_mail(), auto_urgency=Urgency.INFO)
        assert record.summary == "Hello"
        record.analysis = MailAnalysis(summary="generated", urgency=Urgency.INFO)
        assert record.summary == "generated" and not record.analysis_is_fallback
        record.analysis = MailAnalysis(
            summary="Hello", urgency=Urgency.INFO, summary_is_fallback=True
        )
        assert record.summary == "Hello" and record.analysis_is_fallback
        legacy = record.model_dump(mode="json")
        legacy["analysis"].pop("summary_is_fallback")
        legacy["processor_notes"] = [
            {
                "processor_id": "pipeline",
                "plugin_id": "core",
                "status": "failed",
                "message": "fallback summary used",
                "started_at": legacy["mail"]["date"],
                "finished_at": legacy["mail"]["date"],
            }
        ]
        restored = MailRecord.model_validate(legacy)
        assert restored.analysis_is_fallback
        assert MailRecord.model_validate_json(restored.model_dump_json()).analysis_is_fallback
        legacy["analysis"]["summary_is_fallback"] = False
        preserved = MailRecord.model_validate(legacy)
        assert not preserved.analysis_is_fallback
        assert not MailRecord.model_validate_json(preserved.model_dump_json()).analysis_is_fallback


class TestMailMessage:
    def test_source_scope_and_uid_changes_keep_mail_identity_consistent(self) -> None:
        mail = make_mail(message_id="same-rfc-id", provider_message_id="INBOX:10:1")
        assert (
            mail.normalized_message_id()
            == mail.model_copy(update={"provider_message_id": "INBOX:11:5"}).normalized_message_id()
        )
        assert (
            mail.normalized_message_id()
            != mail.model_copy(update={"provider": "different-source"}).normalized_message_id()
        )

    def test_normalized_message_id_uses_provider_id(self) -> None:
        mail = make_mail(message_id="", provider_message_id="provider-42")
        assert mail.normalized_message_id() == mail.model_copy().normalized_message_id()
        assert (
            mail.normalized_message_id()
            != mail.model_copy(update={"provider_message_id": "other"}).normalized_message_id()
        )

    def test_normalized_message_id_digest_without_provider_id(self) -> None:
        mail = make_mail(message_id="")  # no provider or RFC id -> content digest
        digest = mail.normalized_message_id()
        assert digest
        assert mail.normalized_message_id() == digest  # stable

    def test_normalized_message_id_scopes_account_and_source(self) -> None:
        """RFC ids and content hashes cannot merge different mailboxes."""
        first = make_mail(message_id="rfc-1", account_id="acct-1")
        forwarded = make_mail(message_id="rfc-1", account_id="acct-2")  # forward keeps the id
        assert forwarded.normalized_message_id() != first.normalized_message_id()
        # The content fallback also respects account scope
        a = make_mail(message_id="", account_id="acct-1", subject="S", body_text="B")
        b = make_mail(message_id="", account_id="acct-2", subject="S", body_text="B")
        assert a.normalized_message_id() != b.normalized_message_id()
        # different content must not collide
        c = make_mail(message_id="", account_id="acct-1", subject="S", body_text="Other")
        assert a.normalized_message_id() != c.normalized_message_id()


class TestConfigEnvInterpolation:
    def test_embedded_placeholder_stays_literal(self, tmp_path: Path) -> None:
        path = tmp_path / "c.toml"
        path.write_text(
            "[[llms]]\nllm_id = 'l1'\nbase_url = 'https://x'\nmodel = 'pre-${VAR}-post'\n",
            encoding="utf-8",
        )
        config = load_config(path)
        assert config.llms[0].model == "pre-${VAR}-post"

    def test_unset_variable_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MF_MISSING_VAR", raising=False)
        path = tmp_path / "c.toml"
        path.write_text(
            "[[llms]]\nllm_id = 'l1'\nbase_url = 'https://x'\napi_key = '${MF_MISSING_VAR}'\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="MF_MISSING_VAR"):
            load_config(path)


class TestConfigValidation:
    def _llms(self) -> list[dict[str, object]]:
        return [
            {
                "llm_id": "primary",
                "base_url": "https://a",
                "model": "m1",
            },
            {
                "llm_id": "backup",
                "base_url": "https://b",
                "model": "m2",
            },
        ]

    def test_unknown_fallback_rejected(self) -> None:
        llms = self._llms()
        llms[0]["fallback"] = ["ghost"]
        with pytest.raises(ValueError, match="ghost"):
            MailFlowConfig.model_validate({"llms": llms})

    def test_multiple_defaults_rejected(self) -> None:
        llms = self._llms()
        llms[0]["default"] = True
        llms[1]["default"] = True
        with pytest.raises(ValueError, match="multiple default"):
            MailFlowConfig.model_validate({"llms": llms})

    def test_processor_unknown_llm_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown llm"):
            MailFlowConfig.model_validate(
                {
                    "llms": self._llms(),
                    "processors": [
                        {
                            "processor_id": "p1",
                            "provider": "some-plugin",
                            "llm": "ghost",
                        }
                    ],
                }
            )

    def test_processor_unknown_fallback_llm_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown fallback llm"):
            MailFlowConfig.model_validate(
                {
                    "llms": self._llms(),
                    "processors": [
                        {
                            "processor_id": "p1",
                            "provider": "some-plugin",
                            "llm": "primary",
                            "fallback_llms": ["ghost"],
                        }
                    ],
                }
            )

    def test_fallback_without_primary_rejected(self) -> None:
        with pytest.raises(ValueError, match="no primary llm"):
            MailFlowConfig.model_validate(
                {
                    "llms": self._llms(),
                    "processors": [
                        {
                            "processor_id": "p1",
                            "provider": "some-plugin",
                            "fallback_llms": ["backup"],
                        }
                    ],
                }
            )

    def test_invalid_log_level_rejected(self) -> None:
        with pytest.raises(ValueError, match="log level"):
            MailFlowConfig.model_validate({"logging": {"level": "LOUD"}})

    def test_disabled_plugin_can_retain_its_allowlist_membership(self) -> None:
        from mailflow.plugins import PluginManager

        config = MailFlowConfig.model_validate({"plugins": {"enabled": ["a"], "disabled": ["a"]}})
        manager = PluginManager(config)
        manager.load("a", lambda: pytest.fail("disabled plugin must not be imported"))
        manager.load("b", lambda: pytest.fail("allowlist must not expand"))
        assert manager.build_registry().snapshots() == []
        assert manager.status("a") == manager.status("b") == "disabled"


class TestLogging:
    """Stage 06: root isolation, queue delivery and secret redaction."""

    def test_root_handler_list_unchanged(self) -> None:
        import logging

        root_before = list(logging.getLogger().handlers)
        runtime = configure_logging(LoggingConfig(console=False, file=False, jsonl=False))
        try:
            root_after = list(logging.getLogger().handlers)
            assert root_before == root_after
            mailflow_logger = logging.getLogger("mailflow")
            assert mailflow_logger.propagate is False
            assert logging.getLogger("mailflow.runtime").propagate is True
            assert logging.getLogger("unrelated").propagate is True
        finally:
            runtime.close()

    def test_secret_redacted_in_message_and_exc_text(self) -> None:
        import logging

        class CaptureHandler(logging.Handler):
            def __init__(self) -> None:
                super().__init__()
                self.records: list[logging.LogRecord] = []

            def emit(self, record: logging.LogRecord) -> None:
                self.records.append(record)

        capture = CaptureHandler()
        runtime = configure_logging(
            LoggingConfig(console=False, file=False, jsonl=False),
            extra_handlers=[capture],
            secrets=["sk-super-secret"],
        )
        try:
            log = logging.getLogger("mailflow.test")
            log.warning("token=%s ok", "sk-super-secret")
            try:
                raise RuntimeError("failed with sk-super-secret in traceback")
            except RuntimeError:
                log.exception("processing failed")
            records = capture.records
            assert len(records) == 2
            assert "sk-super-secret" not in records[0].getMessage()
            assert "***" in records[0].getMessage()
            formatted = records[1].getMessage() + (records[1].exc_text or "")
            assert "sk-super-secret" not in formatted
        finally:
            runtime.close()

    def test_reconfigure_and_closing_old_logging_do_not_duplicate_or_steal_records(self) -> None:
        import io
        import logging

        first_output, second_output = io.StringIO(), io.StringIO()
        first_handler = logging.StreamHandler(first_output)
        second_handler = logging.StreamHandler(second_output)
        config = LoggingConfig(console=False, file=False, jsonl=False)
        first = configure_logging(config, extra_handlers=[first_handler])
        log = logging.getLogger("mailflow.test")
        log.warning("before replacement")
        second = configure_logging(config, extra_handlers=[second_handler])
        try:
            first.close()
            log.warning("after replacement")
            assert first_output.getvalue().splitlines() == ["before replacement"]
            assert second_output.getvalue().splitlines() == ["after replacement"]
        finally:
            second.close()
            first.close()


class TestConfigPersistenceSecrets:
    """Writing the config back must never materialize a resolved secret."""

    def test_api_key_env_not_persisted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MF_ENV_TOKEN", "sk-env-value")
        config = MailFlowConfig.model_validate(
            {"llms": [{"llm_id": "l1", "api_key_env": "MF_ENV_TOKEN"}]}
        )
        assert config.llms[0].api_key == "sk-env-value"

        target = tmp_path / "out.toml"
        write_config(config, target)
        written = target.read_text(encoding="utf-8")
        assert "sk-env-value" not in written
        assert 'api_key_env = "MF_ENV_TOKEN"' in written

    def test_scalar_set_keeps_placeholders_for_full_rewrites(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`config set` builds a new config via model_validate; the
        placeholder map must survive so a later full rewrite (the section
        being absent from the file) still restores ${VAR}, not the secret."""
        monkeypatch.setenv("MF_SET_TOKEN", "sk-set-value")
        source = tmp_path / "in.toml"
        source.write_text(
            "[[llms]]\nllm_id = 'l1'\nmodel = 'pre-${LITERAL}-post'\n"
            "api_key = '${MF_SET_TOKEN}'\n"
            "[llms.extra_body.auth]\ntoken = '${MF_SET_TOKEN}'\n",
            encoding="utf-8",
        )
        config = load_config(source)
        assert config.llms[0].api_key == "sk-set-value"
        assert config.llms[0].model == "pre-${LITERAL}-post"
        assert config.llms[0].extra_body["auth"]["token"] == "sk-set-value"
        updated = set_option_value(config, "general.timezone", "Asia/Shanghai")
        target = tmp_path / "out.toml"
        write_config(updated, target)
        written = target.read_text(encoding="utf-8")
        assert "sk-set-value" not in written
        assert "${MF_SET_TOKEN}" in written
        restored = load_config(target)
        assert restored.llms[0] == config.llms[0]
        assert restored.general.timezone == "Asia/Shanghai"

    def test_scalar_set_drops_placeholder_of_the_patched_leaf(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Replacing a ${VAR} leaf with a literal must persist the literal;
        the stale placeholder entry would otherwise resurrect ${VAR}."""
        monkeypatch.setenv("MF_TZ", "Asia/Shanghai")
        source = tmp_path / "in.toml"
        source.write_text(
            '[general]\ntimezone = "${MF_TZ}"\n',
            encoding="utf-8",
        )
        config = load_config(source)
        assert config.general.timezone == "Asia/Shanghai"
        updated = set_option_value(config, "general.timezone", "UTC")
        target = tmp_path / "out.toml"
        write_config(updated, target)
        written = target.read_text(encoding="utf-8")
        assert 'timezone = "UTC"' in written
        assert "MF_TZ" not in written


class TestUniqueIdValidation:
    def test_duplicate_llm_ids_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate llm_id"):
            MailFlowConfig.model_validate({"llms": [{"llm_id": "a"}, {"llm_id": "a"}]})

    def test_duplicate_processor_ids_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate processor_id"):
            MailFlowConfig.model_validate(
                {
                    "processors": [
                        {"processor_id": "p", "provider": "rules"},
                        {"processor_id": "p", "provider": "rules"},
                    ]
                }
            )

    def test_duplicate_account_and_notifier_ids_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate account_id"):
            MailFlowConfig.model_validate(
                {
                    "accounts": [
                        {"account_id": "x", "provider": "fake"},
                        {"account_id": "x", "provider": "fake"},
                    ]
                }
            )
        with pytest.raises(ValueError, match="duplicate notifier_id"):
            MailFlowConfig.model_validate(
                {
                    "notifiers": [
                        {"notifier_id": "n", "provider": "console"},
                        {"notifier_id": "n", "provider": "console"},
                    ]
                }
            )


class TestPatchConfigValue:
    """In-place patching is section-scoped: a same-named leaf elsewhere is safe."""

    def test_patches_the_named_section(self, tmp_path: Path) -> None:
        path = tmp_path / "c.toml"
        path.write_text("[general]\nlanguage = 'en'\n\n[i18n]\nlanguage = 'en'\n", encoding="utf-8")
        assert patch_config_value(path, "i18n.language", "zh-CN") is True
        text = path.read_text(encoding="utf-8")
        general, i18n = text.split("[i18n]")
        assert "'en'" in general  # [general] untouched
        assert '"zh-CN"' in i18n

    def test_missing_section_is_not_patched_elsewhere(self, tmp_path: Path) -> None:
        path = tmp_path / "c.toml"
        path.write_text("[i18n]\nlanguage = 'en'\n", encoding="utf-8")
        # [general] is absent: the caller must fall back to a full rewrite
        # instead of clobbering [i18n].language
        assert patch_config_value(path, "general.language", "zh-CN") is False
        assert path.read_text(encoding="utf-8") == "[i18n]\nlanguage = 'en'\n"

    def test_headerless_file_is_searched_whole(self, tmp_path: Path) -> None:
        path = tmp_path / "c.toml"
        path.write_text("language = 'en'\n", encoding="utf-8")
        assert patch_config_value(path, "general.language", "zh-CN") is True
        assert path.read_text(encoding="utf-8") == 'language = "zh-CN"\n'
