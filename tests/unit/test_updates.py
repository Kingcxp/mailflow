"""Version checking and updates: MailFlow releases, plugin marketplace
versions, update-source tracking and the daily auto-update loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from mailflow.commands import CommandRouter
from mailflow.plugin_market import MarketPlugin, PluginMarket, Repository
from mailflow.updates import (
    UpdateReport,
    check_plugin_updates,
    check_updates,
    latest_mailflow_release,
)


class _PrefStorage:
    """Minimal storage: update flows only touch preferences."""

    def __init__(self) -> None:
        self.preferences: dict[str, str] = {}

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def get_preference(self, key: str) -> str | None:
        return self.preferences.get(key)

    async def set_preference(self, key: str, value: str) -> None:
        self.preferences[key] = value


def _make_service(config_path: str | None = None) -> Any:
    from mailflow.config import MailFlowConfig
    from mailflow.events import EventBus
    from mailflow.i18n import I18n
    from mailflow.pipeline import PipelineEngine
    from mailflow.plugins import PluginManager
    from mailflow.registry import ComponentRegistry
    from mailflow.service import MailFlowService

    service = MailFlowService(
        config=MailFlowConfig(),
        registry=ComponentRegistry(),
        plugin_manager=PluginManager(),
        storage=cast(Any, _PrefStorage()),
        sources={},
        router=cast(Any, None),
        pipeline=PipelineEngine([]),
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        i18n=I18n(),
    )
    if config_path:
        service.config_path = config_path  # pyright: ignore[reportAttributeAccessIssue]
    return service


class FakeMarket(PluginMarket):
    """Market with one known plugin (or none for missing ids)."""

    def __init__(self, plugin: MarketPlugin | None) -> None:
        super().__init__([])
        self._plugin = plugin

    def find(  # type: ignore[override]
        self, plugin_id: str, timeout: float = 15.0
    ) -> tuple[Repository, MarketPlugin] | None:
        if self._plugin is not None and self._plugin.id == plugin_id:
            return Repository(name="local", url="file:///tmp"), self._plugin
        return None


def _plugin(plugin_id: str = "mailflow-notify-demo", version: str = "1.0.0") -> MarketPlugin:
    return MarketPlugin(
        id=plugin_id,
        name="Demo",
        version=version,
        categories=["notifier"],
        package=plugin_id,
        source=f"git+https://example.com/repo.git#{plugin_id}",
    )


@pytest.fixture
def commands() -> tuple[CommandRouter, Any]:
    service = _make_service()
    return CommandRouter(service), service


class TestLatestRelease:
    def test_returns_tag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "mailflow.updates._fetch_json",
            lambda url: {"tag_name": "v2.3.4"},  # type: ignore[arg-type]
        )
        assert latest_mailflow_release("0.1.0") == "2.3.4"

    def test_unreachable_keeps_current(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(url: str) -> Any:
            raise RuntimeError("network down")

        monkeypatch.setattr("mailflow.updates._fetch_json", boom)
        assert latest_mailflow_release("0.1.0") == "0.1.0"


class TestCheckPluginUpdates:
    def test_remote_source_with_newer_version(self) -> None:
        installed = {"mailflow-notify-demo": "0.9.0"}
        sources = {"mailflow-notify-demo": "git+https://example.com/repo.git"}
        updates = check_plugin_updates(FakeMarket(_plugin(version="1.0.0")), installed, sources)
        assert updates == {"mailflow-notify-demo": ("0.9.0", "1.0.0")}

    def test_local_or_missing_source_never_auto_updates(self) -> None:
        installed = {"mailflow-notify-demo": "0.9.0"}
        assert (
            check_plugin_updates(
                FakeMarket(_plugin(version="1.0.0")), installed, {"mailflow-notify-demo": ""}
            )
            == {}
        )
        assert (
            check_plugin_updates(
                FakeMarket(_plugin(version="1.0.0")),
                installed,
                {"mailflow-notify-demo": "C:/dev/plugin"},
            )
            == {}
        )

    def test_repository_gone_skipped(self) -> None:
        installed = {"mailflow-notify-gone": "0.9.0"}
        sources = {"mailflow-notify-gone": "git+https://example.com/repo.git"}
        assert check_plugin_updates(FakeMarket(None), installed, sources) == {}

    def test_same_version_no_update(self) -> None:
        installed = {"mailflow-notify-demo": "1.0.0"}
        sources = {"mailflow-notify-demo": "git+https://example.com/repo.git"}
        assert check_plugin_updates(FakeMarket(_plugin(version="1.0.0")), installed, sources) == {}


class TestCheckUpdates:
    def test_report_combines_mailflow_and_plugins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "mailflow.updates.latest_mailflow_release",
            lambda current: "9.9.9",  # type: ignore[arg-type]
        )
        installed = {"mailflow-notify-demo": "0.9.0"}
        sources = {"mailflow-notify-demo": "git+https://example.com/repo.git"}
        report = check_updates(
            FakeMarket(_plugin(version="1.0.0")),
            installed_plugins=installed,
            sources=sources,
            mailflow_current="0.1.0",
        )
        assert report.mailflow_update is True
        assert report.plugin_updates == {"mailflow-notify-demo": ("0.9.0", "1.0.0")}
        assert report.has_updates is True

    def test_no_updates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "mailflow.updates.latest_mailflow_release",
            lambda current: current,  # type: ignore[arg-type]
        )
        report = check_updates(
            FakeMarket(None), installed_plugins={}, sources={}, mailflow_current="0.1.0"
        )
        assert report.has_updates is False


class TestUpdateCommand:
    async def test_update_check_reports(
        self, commands: tuple[CommandRouter, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        router, _service = commands
        monkeypatch.setattr(  # type: ignore[arg-type]
            "mailflow.updates.check_updates",
            lambda *a, **k: UpdateReport(  # type: ignore[arg-type, reportUnknownLambdaType]
                mailflow_current="0.1.0",
                mailflow_latest="9.9.9",
                mailflow_update=True,
                plugin_updates={"mailflow-notify-demo": ("0.9.0", "1.0.0")},
            ),
        )
        response = await router.execute("update check")
        assert response.ok, response.text
        assert "9.9.9" in response.text
        assert "mailflow-notify-demo" in response.text

    async def test_update_status(self, commands: tuple[CommandRouter, Any]) -> None:
        router, _service = commands
        status = await router.execute("update status")
        assert status.ok
        assert "auto update: yes" in status.text

    async def test_update_auto_toggle_persists(self, tmp_path: Path) -> None:
        service = _make_service(config_path=str(tmp_path / "config.toml"))
        router = CommandRouter(service)
        toggled = await router.execute("update auto off")
        assert toggled.ok, toggled.text
        assert service.config.general.auto_update is False
        assert "auto update: no" in (await router.execute("update status")).text


class TestDailyUpdateLoop:
    async def test_runs_once_per_day_and_emits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service = _make_service()
        events_captured: list[tuple[str, dict[str, Any]]] = []

        async def capture(event: str, **payload: Any) -> None:
            events_captured.append((event, payload))

        service.events.subscribe("mailflow.update.checked", capture)
        service.events.subscribe("mailflow.update.applied", capture)
        report = UpdateReport(
            mailflow_current="0.1.0",
            mailflow_latest="2.0.0",
            mailflow_update=True,
            plugin_updates={"p": ("0.9.0", "1.0.0")},
        )

        async def fake_check() -> UpdateReport:
            return report

        async def fake_apply() -> dict[str, str]:
            return {"mailflow": "updated"}

        monkeypatch.setattr(service, "check_updates", fake_check)
        monkeypatch.setattr(service, "apply_updates", fake_apply)
        await service._run_daily_update()  # pyright: ignore[reportPrivateUsage]
        assert events_captured[0][0] == "mailflow.update.checked"
        assert events_captured[1][0] == "mailflow.update.applied"
        storage = service.storage
        assert any(k.startswith("update.check.") for k in storage.preferences)  # pyright: ignore[reportUnknownMemberType]
        # once per day
        events_captured.clear()
        await service._run_daily_update()  # pyright: ignore[reportPrivateUsage]
        assert events_captured == []

    async def test_disabled_auto_update_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service = _make_service()
        service.config.general.auto_update = False

        async def should_not_run() -> UpdateReport:
            raise AssertionError("check_updates must not run when auto update is off")

        monkeypatch.setattr(service, "check_updates", should_not_run)
        await service._run_daily_update()  # pyright: ignore[reportPrivateUsage]
        storage = service.storage
        assert not storage.preferences  # pyright: ignore[reportUnknownMemberType]
