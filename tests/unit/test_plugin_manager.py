"""Plugin discovery resilience and marketplace metadata tolerance."""

from __future__ import annotations

from typing import Any

import pytest
from mailflow.config import MailFlowConfig
from mailflow.plugin_market import MarketPlugin, PluginMarket, Repository
from mailflow.plugins import PluginManager


class BrokenInfoPlugin:
    """A plugin whose info hook explodes with something other than
    AttributeError — must cost the plugin itself, never the host."""

    def mailflow_plugin_info(self) -> None:
        raise KeyError("id")


class BrokenRegisterPlugin:
    def mailflow_plugin_info(self) -> Any:
        from mailflow.plugins import PluginInfo

        return PluginInfo(plugin_id="mailflow-broken-register")

    def mailflow_register(self, registrar: Any, config: MailFlowConfig) -> None:
        raise RuntimeError("boom")


class TestBrokenPluginsDoNotKillStartup:
    def test_broken_info_hook_is_skipped(self) -> None:
        manager = PluginManager(MailFlowConfig())
        assert manager.register(BrokenInfoPlugin()) is None
        assert manager.plugin_ids == []

    def test_failing_register_is_isolated(self) -> None:
        manager = PluginManager(MailFlowConfig())
        manager.register(BrokenRegisterPlugin())
        registry = manager.build_registry()  # must not raise
        assert registry.snapshots() == []


class TestMarketplaceMetadataTolerance:
    @staticmethod
    def _market() -> PluginMarket:
        return PluginMarket([Repository(name="repo", url="https://example.com/mirror")])

    def test_github_web_url_fetches_files_from_raw(self) -> None:
        file_base = PluginMarket._file_base  # pyright: ignore[reportPrivateUsage]
        assert (
            file_base("https://github.com/Kingcxp/mailflow-repo")
            == "https://raw.githubusercontent.com/Kingcxp/mailflow-repo/main"
        )
        assert (
            file_base("https://github.com/o/r/tree/dev")
            == "https://raw.githubusercontent.com/o/r/dev"
        )
        # non-github URLs pass through untouched (raw/INDEX.json mirrors)
        assert (
            file_base("https://raw.githubusercontent.com/o/r/main")
            == "https://raw.githubusercontent.com/o/r/main"
        )

    def test_one_bad_plugin_json_does_not_hide_the_repository(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good = MarketPlugin(id="mailflow-good", version="1.0.0", package="mailflow-good")
        payloads: dict[str, Any] = {
            "https://example.com/mirror/index.json": {
                "categories": [{"id": "notifier", "path": "notifier"}]
            },
            "https://example.com/mirror/notifier/mailflow-good/plugin.json": good.model_dump(),
            "https://example.com/mirror/notifier/mailflow-bad/plugin.json": {"version": "1.0.0"},
        }

        def fake_fetch(url: str, timeout: float = 15.0) -> Any:
            return payloads[url]

        market = self._market()
        monkeypatch.setattr("mailflow.plugin_market._fetch_json", fake_fetch)  # pyright: ignore[reportPrivateUsage]

        def fake_dirs(base: str, category: str, timeout: float) -> list[str]:
            return ["mailflow-bad", "mailflow-good"]

        monkeypatch.setattr(
            market,
            "_list_plugin_dirs",
            fake_dirs,
        )  # pyright: ignore[reportPrivateUsage]
        entries = [plugin.id for _repo, plugin in market.list_plugins()]
        assert entries == ["mailflow-good"]
