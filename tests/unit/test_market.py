"""Unit tests for the plugin marketplace module."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from mailflow.plugin_market import MarketPlugin, PluginMarket, Repository

INDEX = {
    "name": "test-market",
    "schema": 2,
    "categories": [{"id": "processor", "path": "processor"}, {"id": "storage", "path": "storage"}],
}

PLUGIN_A = {
    "id": "mailflow-test-plugin",
    "name": "Test Plugin",
    "version": "1.2.3",
    "description": "A plugin used in tests",
    "categories": ["processor", "experimental"],
    "package": "mailflow-test-plugin",
    "source": "https://example.invalid/mailflow-test-plugin",
    "author": "tester",
    "license": "MIT",
    "readme": "## Test Plugin\n\nLong markdown description.",
}

PLUGIN_B = {
    "id": "mailflow-testkit",
    "name": "Testkit",
    "version": "0.1.0",
    "description": "Already installed distribution",
    "categories": ["storage"],
    "package": "mailflow-testkit",
    "source": "mailflow-testkit",
}


def _write_repo(tmp_path: Path) -> Path:
    (tmp_path / "processor" / "mailflow-test-plugin").mkdir(parents=True)
    (tmp_path / "storage" / "mailflow-testkit").mkdir(parents=True)
    (tmp_path / "index.json").write_text(json.dumps(INDEX), encoding="utf-8")
    (tmp_path / "processor" / "mailflow-test-plugin" / "plugin.json").write_text(
        json.dumps(PLUGIN_A), encoding="utf-8"
    )
    (tmp_path / "storage" / "mailflow-testkit" / "plugin.json").write_text(
        json.dumps(PLUGIN_B), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def market(tmp_path: Path) -> PluginMarket:
    return PluginMarket([Repository("local", _write_repo(tmp_path).as_uri())])


class TestPluginMarket:
    def test_fetch_and_list(self, market: PluginMarket) -> None:
        entries = market.list_plugins()
        assert len(entries) == 2
        repo, plugin = entries[0]
        assert repo.name == "local"
        assert plugin.id == "mailflow-test-plugin"
        assert plugin.categories == ["processor", "experimental"]
        assert plugin.description == "A plugin used in tests"
        assert plugin.readme.startswith("## Test Plugin")

    def test_find(self, market: PluginMarket) -> None:
        found = market.find("mailflow-test-plugin")
        assert found is not None
        _repo, plugin = found
        assert plugin.version == "1.2.3"
        assert market.find("ghost") is None

    def test_failing_repository_is_skipped(self, tmp_path: Path) -> None:
        good = _write_repo(tmp_path / "good")
        market = PluginMarket(
            [
                Repository("broken", (tmp_path / "missing").as_uri()),
                Repository("local", good.as_uri()),
            ]
        )
        entries = market.list_plugins()
        assert len(entries) == 2  # broken repo logged, good repo served

    def test_broken_metadata_file_is_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "processor" / "bad").mkdir(parents=True)
        (tmp_path / "index.json").write_text(json.dumps(INDEX), encoding="utf-8")
        (tmp_path / "processor" / "bad" / "plugin.json").write_text("{not json", encoding="utf-8")
        market = PluginMarket([Repository("local", tmp_path.as_uri())])
        assert market.list_plugins() == []

    def test_search_filters_by_query_and_category(self, market: PluginMarket) -> None:
        assert len(market.search("tests")) == 1
        assert len(market.search("", "storage")) == 1
        assert market.search("tests", "storage") == []

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
