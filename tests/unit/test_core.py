"""Unit tests for the MailFlow core domain contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mailflow.config import LoggingConfig, MailFlowConfig, load_config
from mailflow.domain import (
    MailAnalysis,
    MailMessage,
    MailRecord,
    Urgency,
    parse_urgency,
)
from mailflow.logging import configure_logging
from pydantic import ValidationError

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

    def test_exactly_four_values(self) -> None:
        assert len(Urgency) == 4

    def test_values(self) -> None:
        assert Urgency.AD.value == "ad"
        assert Urgency.INFO.value == "info"
        assert Urgency.IMPORTANT.value == "important"
        assert Urgency.URGENT.value == "urgent"

    def test_colors(self) -> None:
        assert Urgency.AD.color == "#909399"  # gray: irrelevant ads
        assert Urgency.INFO.color == "#67C23A"  # green: useful, not urgent
        assert Urgency.IMPORTANT.color == "#E6A23C"  # orange: important, read it
        assert Urgency.URGENT.color == "#F56C6C"  # red: must handle

    def test_rank_order(self) -> None:
        assert Urgency.AD.rank < Urgency.INFO.rank < Urgency.IMPORTANT.rank < Urgency.URGENT.rank

    def test_parse_synonyms(self) -> None:
        assert parse_urgency("urgent") is Urgency.URGENT
        assert parse_urgency("critical") is Urgency.URGENT
        assert parse_urgency("junk") is Urgency.AD
        assert parse_urgency("ads") is Urgency.AD
        assert parse_urgency("medium") is Urgency.IMPORTANT
        assert parse_urgency("bogus-value") is Urgency.INFO
        assert parse_urgency(None) is Urgency.INFO


class TestMailRecordUrgency:
    def test_effective_defaults_to_automatic(self) -> None:
        record = MailRecord(
            record_id="r1",
            mail=make_mail(),
            auto_urgency=Urgency.IMPORTANT,
        )
        assert record.effective_urgency is Urgency.IMPORTANT

    def test_manual_wins_over_automatic(self) -> None:
        record = MailRecord(
            record_id="r1",
            mail=make_mail(),
            auto_urgency=Urgency.AD,
            manual_urgency=Urgency.URGENT,
        )
        assert record.effective_urgency is Urgency.URGENT

    def test_reset_restores_automatic(self) -> None:
        record = MailRecord(
            record_id="r1",
            mail=make_mail(),
            auto_urgency=Urgency.IMPORTANT,
            manual_urgency=Urgency.URGENT,
        )
        assert record.effective_urgency is Urgency.URGENT
        record.manual_urgency = None
        assert record.effective_urgency is Urgency.IMPORTANT

    def test_manual_never_overwrites_automatic(self) -> None:
        record = MailRecord(
            record_id="r1",
            mail=make_mail(),
            auto_urgency=Urgency.URGENT,
            manual_urgency=Urgency.AD,
        )
        assert record.auto_urgency is Urgency.URGENT
        assert record.effective_urgency is Urgency.AD

    def test_summary_falls_back_to_subject(self) -> None:
        record = MailRecord(record_id="r1", mail=make_mail(), auto_urgency=Urgency.INFO)
        assert record.summary == "Hello"

    def test_summary_from_analysis(self) -> None:
        record = MailRecord(
            record_id="r1",
            mail=make_mail(),
            auto_urgency=Urgency.INFO,
            analysis=MailAnalysis(summary="the summary", urgency=Urgency.INFO),
        )
        assert record.summary == "the summary"

    def test_serialization_roundtrip(self) -> None:
        record = MailRecord(
            record_id="r1",
            mail=make_mail(),
            auto_urgency=Urgency.URGENT,
            manual_urgency=Urgency.IMPORTANT,
            analysis=MailAnalysis(summary="s", urgency=Urgency.URGENT, reason="r"),
        )
        restored = MailRecord.model_validate_json(record.model_dump_json())
        assert restored == record
        assert restored.effective_urgency is Urgency.IMPORTANT


class TestMailMessage:
    def test_normalized_message_id_uses_provider_id(self) -> None:
        mail = make_mail(provider_message_id="provider-42")
        assert mail.normalized_message_id() == "provider-42"

    def test_normalized_message_id_digest_without_provider_id(self) -> None:
        mail = make_mail()
        digest = mail.normalized_message_id()
        assert len(digest) == 24
        assert mail.normalized_message_id() == digest  # stable

    def test_invalid_urgency_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MailAnalysis.model_validate({"summary": "x", "urgency": "not-a-value"})


class TestConfigDefaults:
    def test_retention_and_cleanup_defaults(self) -> None:
        config = MailFlowConfig()
        assert config.general.language == "en"
        assert config.general.timezone == "UTC"
        assert config.general.mail_retention_days == 30
        assert config.general.trash_retention_days == 7
        assert config.general.cleanup_hour == 4
        assert config.general.cleanup_minute == 0
        assert config.general.queue_size == 500
        assert config.general.workers == 2

    def test_storage_defaults(self) -> None:
        config = MailFlowConfig()
        assert config.storage.provider == "mailflow-storage-sqlite"
        assert config.storage.path == "data/mailflow.db"


class TestConfigEnvInterpolation:
    def test_whole_string_placeholder_expanded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MF_TEST_TOKEN", "secret-value")
        path = tmp_path / "c.toml"
        path.write_text(
            "[[llms]]\nllm_id = 'l1'\nbase_url = 'https://x'\napi_key = '${MF_TEST_TOKEN}'\n",
            encoding="utf-8",
        )
        config = load_config(path)
        assert config.llms[0].api_key == "secret-value"

    def test_embedded_placeholder_stays_literal(self, tmp_path: Path) -> None:
        path = tmp_path / "c.toml"
        path.write_text(
            "[[llms]]\nllm_id = 'l1'\nbase_url = 'https://x'\nmodel = 'pre-${VAR}-post'\n",
            encoding="utf-8",
        )
        config = load_config(path)
        assert config.llms[0].model == "pre-${VAR}-post"

    def test_unset_variable_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_unknown_timezone_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            MailFlowConfig.model_validate({"general": {"timezone": "Not/AZone"}})

    def test_invalid_log_level_rejected(self) -> None:
        with pytest.raises(ValueError, match="log level"):
            MailFlowConfig.model_validate({"logging": {"level": "LOUD"}})

    def test_plugin_enabled_disabled_overlap_rejected(self) -> None:
        with pytest.raises(ValueError, match="both enabled and disabled"):
            MailFlowConfig.model_validate({"plugins": {"enabled": ["a"], "disabled": ["a"]}})

    def test_default_llm_accessor(self) -> None:
        llms = self._llms()
        llms[0]["default"] = True
        config = MailFlowConfig.model_validate({"llms": llms})
        assert config.default_llm() is not None
        assert config.default_llm().llm_id == "primary"  # type: ignore[union-attr]

    def test_load_config_from_file(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            "[general]\nlanguage = 'zh-CN'\nmail_retention_days = 14\n\n"
            "[[llms]]\nllm_id = 'l1'\nbase_url = 'https://x'\nmodel = 'm'\n",
            encoding="utf-8",
        )
        config = load_config(path)
        assert config.general.language == "zh-CN"
        assert config.general.mail_retention_days == 14
        assert config.llms[0].llm_id == "l1"


class TestLogging:
    """Stage 06: root isolation, queue delivery and secret redaction."""

    def _capture_config(self) -> tuple[LoggingConfig, list[str]]:
        captured: list[str] = []
        config = LoggingConfig(
            console=False,
            file=False,
            jsonl=False,
            level="DEBUG",
            logger_levels={},
        )
        return config, captured

    def test_root_handler_list_unchanged(self) -> None:
        import logging

        root_before = list(logging.getLogger().handlers)
        runtime = configure_logging(LoggingConfig(console=False, file=False, jsonl=False))
        try:
            root_after = list(logging.getLogger().handlers)
            assert root_before == root_after
            mailflow_logger = logging.getLogger("mailflow")
            assert mailflow_logger.propagate is False
        finally:
            runtime.close()

    def test_propagate_false_scoped_to_mailflow(self) -> None:
        import logging

        runtime = configure_logging(LoggingConfig(console=False, file=False, jsonl=False))
        try:
            assert logging.getLogger("mailflow").propagate is False
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

    def test_double_configure_replaces_cleanly(self) -> None:
        import logging
        import logging.handlers

        first = configure_logging(LoggingConfig(console=False, file=False, jsonl=False))
        second = configure_logging(LoggingConfig(console=False, file=False, jsonl=False))
        try:
            mailflow_logger = logging.getLogger("mailflow")
            queue_handlers = [
                h for h in mailflow_logger.handlers if isinstance(h, logging.handlers.QueueHandler)
            ]
            null_handlers = [
                h for h in mailflow_logger.handlers if isinstance(h, logging.NullHandler)
            ]
            # exactly one queue chain; the NullHandler bootstrap guard stays
            assert len(queue_handlers) == 1
            assert len(null_handlers) == 1
        finally:
            second.close()
            first.close()
