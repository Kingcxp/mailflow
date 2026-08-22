"""Unit tests for the TUI runner's terminal isolation and service wiring."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mailflow.config import MailFlowConfig
from mailflow_tui import runner as runner_module
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


class TestRunTuiWiring:
    """The TUI must be able to persist config changes: it needs config_path."""

    def test_config_path_forwarded_to_start_service(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "c.toml"
        config_file.write_text("[general]\ntimezone = 'UTC'\n", encoding="utf-8")
        captured: dict[str, Any] = {}

        class StubApp:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None: ...

            async def run_async(self) -> None:
                return None

        class StubService:
            config_path: Path | None = None

            async def stop(self) -> None:
                return None

        async def fake_start_service(config: Any, **kwargs: Any) -> StubService:
            captured.update(kwargs)
            return StubService()

        monkeypatch.setattr(runner_module, "start_service", fake_start_service)

        def stub_router(service: Any) -> None:
            return None

        monkeypatch.setattr(runner_module, "CommandRouter", stub_router)
        monkeypatch.setitem(
            __import__("sys").modules,
            "mailflow_tui.app",
            type("_M", (), {"MailFlowApp": StubApp}),
        )

        runner_module.run_tui(str(config_file))
        assert captured["config_path"] == str(config_file)


class TestEnsureDefaultConfig:
    def test_missing_file_is_bootstrapped_from_example(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mailflow_tui.runner import ensure_default_config

        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "example.toml").write_text("[general]\n", encoding="utf-8")

        target = str(tmp_path / "configs" / "development.toml")
        result = ensure_default_config(target)

        assert result == target
        assert Path(result).is_file()

    def test_existing_file_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mailflow_tui.runner import ensure_default_config

        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "development.toml").write_text(
            '[general]\ntimezone = "Asia/Shanghai"\n', encoding="utf-8"
        )
        (tmp_path / "configs" / "example.toml").write_text("[general]\n", encoding="utf-8")

        target = str(tmp_path / "configs" / "development.toml")
        assert ensure_default_config(target) == target
        assert 'timezone = "Asia/Shanghai"' in Path(target).read_text(encoding="utf-8")

    def test_no_example_means_no_bootstrap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mailflow_tui.runner import ensure_default_config

        monkeypatch.chdir(tmp_path)
        target = str(tmp_path / "configs" / "development.toml")
        assert ensure_default_config(target) == target
        assert not Path(target).exists()
