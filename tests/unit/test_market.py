"""Unit tests for the plugin marketplace module."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from mailflow.plugin_market import MarketPlugin, PluginMarket, Repository

INDEX = {
    "name": "test-market",
    "plugins": [
        {
            "id": "mailflow-test-plugin",
            "name": "Test Plugin",
            "version": "1.2.3",
            "description": "A plugin used in tests",
            "categories": ["processor", "experimental"],
            "package": "mailflow-test-plugin",
            "source": "https://example.invalid/mailflow-test-plugin",
            "author": "tester",
            "license": "MIT",
        },
        {
            "id": "mailflow-testkit",
            "name": "Testkit",
            "version": "0.1.0",
            "description": "Already installed distribution",
            "categories": ["storage"],
            "package": "mailflow-testkit",
            "source": "mailflow-testkit",
        },
    ],
}


@pytest.fixture
def index_path(tmp_path: Path) -> Path:
    path = tmp_path / "plugins.json"
    path.write_text(json.dumps(INDEX), encoding="utf-8")
    return path


@pytest.fixture
def market(index_path: Path) -> PluginMarket:
    return PluginMarket([Repository("local", index_path.as_uri())])


class TestPluginMarket:
    def test_fetch_and_list(self, market: PluginMarket) -> None:
        entries = market.list_plugins()
        assert len(entries) == 2
        repo, plugin = entries[0]
        assert repo.name == "local"
        assert plugin.id == "mailflow-test-plugin"
        assert plugin.categories == ["processor", "experimental"]
        assert plugin.description == "A plugin used in tests"

    def test_find(self, market: PluginMarket) -> None:
        found = market.find("mailflow-test-plugin")
        assert found is not None
        _repo, plugin = found
        assert plugin.version == "1.2.3"
        assert market.find("ghost") is None

    def test_failing_repository_is_skipped(self, tmp_path: Path) -> None:
        market = PluginMarket(
            [
                Repository("broken", (tmp_path / "missing.json").as_uri()),
                Repository("local", (tmp_path / "ok.json").as_uri()),
            ]
        )
        (tmp_path / "ok.json").write_text(json.dumps(INDEX), encoding="utf-8")
        entries = market.list_plugins()
        assert len(entries) == 2  # broken repo logged, local repo served

    def test_is_installed_by_distribution_package(self) -> None:
        assert PluginMarket.is_installed("anything", package="mailflow-testkit") is True
        assert PluginMarket.is_installed("ghost", package="no-such-package-xyz") is False

    def test_install_already_installed_shortcut(self, market: PluginMarket) -> None:
        plugin = MarketPlugin(
            id="mailflow-testkit",
            name="Testkit",
            package="mailflow-testkit",
            source="mailflow-testkit",
        )
        result = asyncio.run(market.install(plugin))
        assert "already installed" in result

    def test_install_without_source_raises(self) -> None:
        plugin = MarketPlugin(id="x", name="x", source="")
        with pytest.raises(ValueError, match="no install source"):
            asyncio.run(PluginMarket([]).install(plugin))
