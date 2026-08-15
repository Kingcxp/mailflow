"""Unit tests for the TUI runner's terminal isolation."""

from __future__ import annotations

from mailflow.config import MailFlowConfig
from mailflow_tui.runner import tui_logging_config


class TestTuiLoggingConfig:
    def test_console_sink_disabled(self) -> None:
        config = MailFlowConfig()
        assert config.logging.console is True
        adjusted = tui_logging_config(config)
        assert adjusted.logging.console is False
        # the caller's config object is untouched
        assert config.logging.console is True

    def test_file_and_jsonl_sinks_kept(self) -> None:
        config = MailFlowConfig.model_validate({"logging": {"file": True, "jsonl": True}})
        adjusted = tui_logging_config(config)
        assert adjusted.logging.file is True
        assert adjusted.logging.jsonl is True
        assert adjusted.logging.console is False

    def test_other_settings_preserved(self) -> None:
        config = MailFlowConfig.model_validate(
            {"general": {"timezone": "Asia/Shanghai"}, "logging": {"level": "DEBUG"}}
        )
        adjusted = tui_logging_config(config)
        assert adjusted.general.timezone == "Asia/Shanghai"
        assert adjusted.logging.level == "DEBUG"
