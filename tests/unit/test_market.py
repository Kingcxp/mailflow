"""Unit tests for the plugin marketplace module."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from mailflow.plugin_market import (
    MarketFetchReport,
    MarketPlugin,
    PluginMarket,
    Repository,
    deserialize_market_cache,
    merge_market_cache,
    serialize_market_cache,
)

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
    "updated": "2026-07-15",
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

    def test_report_deduplicates_plugin_id_across_categories(self, tmp_path: Path) -> None:
        repository_root = tmp_path / "duplicate"
        plugin_second = {**PLUGIN_A, "name": "Second category copy"}
        (repository_root / "processor" / "mailflow-test-plugin").mkdir(parents=True)
        (repository_root / "storage" / "mailflow-test-plugin").mkdir(parents=True)
        (repository_root / "index.json").write_text(json.dumps(INDEX), encoding="utf-8")
        (repository_root / "processor" / "mailflow-test-plugin" / "plugin.json").write_text(
            json.dumps(PLUGIN_A), encoding="utf-8"
        )
        (repository_root / "storage" / "mailflow-test-plugin" / "plugin.json").write_text(
            json.dumps(plugin_second), encoding="utf-8"
        )
        market = PluginMarket([Repository("duplicate", repository_root.as_uri())])

        report = market.list_plugins_report()

        assert report.failures == []
        assert len(report.entries) == 1
        assert report.entries[0][1].name == "Test Plugin"
        assert market.list_plugins() == report.entries

    def test_report_keeps_configured_order_and_first_duplicate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broken = Repository("broken", "https://broken.invalid")
        first = Repository("first", "https://first.invalid")
        second = Repository("second", "https://second.invalid")
        market = PluginMarket([broken, first, second])
        first_plugin = MarketPlugin.model_validate(PLUGIN_A)
        duplicate_plugin = MarketPlugin.model_validate({**PLUGIN_A, "name": "Duplicate"})
        second_plugin = MarketPlugin.model_validate(PLUGIN_B)

        def fetch(repository: Repository, _timeout: float) -> list[MarketPlugin]:
            if repository == broken:
                raise RuntimeError(
                    "access_token=private-token Authorization: Bearer auth-secret "
                    "https://user:password@host.invalid"
                )
            if repository == first:
                return [first_plugin]
            return [duplicate_plugin, second_plugin]

        monkeypatch.setattr(market, "fetch_index", fetch)
        report = market.list_plugins_report()

        assert [repository.name for repository, _plugin in report.entries] == ["first", "second"]
        assert report.entries[0][1].name == first_plugin.name
        assert len(report.failures) == 1
        failed_repository, reason = report.failures[0]
        assert failed_repository == broken
        assert "private-token" not in reason
        assert "user:password" not in reason
        assert "auth-secret" not in reason

    def test_report_contains_status_for_every_repository(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good = Repository("good", "https://good.invalid")
        broken = Repository("broken", "https://broken.invalid")
        market = PluginMarket([good, broken])
        plugin = MarketPlugin.model_validate(PLUGIN_A)

        def fetch(repository: Repository, _timeout: float) -> list[MarketPlugin]:
            if repository == broken:
                raise RuntimeError("offline")
            return [plugin]

        monkeypatch.setattr(market, "fetch_index", fetch)
        report = market.list_plugins_report()

        assert [
            (status.repository.name, status.ok, status.plugin_count)
            for status in report.repositories
        ] == [
            ("good", True, 1),
            ("broken", False, 0),
        ]
        assert report.repositories[1].error == "offline"

    def test_find_and_search_use_deduplicated_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first = Repository("first", "https://first.invalid")
        market = PluginMarket([first])
        plugin = MarketPlugin.model_validate(PLUGIN_A)

        def duplicate_fetch(_repository: Repository, _timeout: float) -> list[MarketPlugin]:
            return [plugin, plugin]

        monkeypatch.setattr(market, "fetch_index", duplicate_fetch)

        assert market.find(plugin.id) == (first, plugin)
        assert market.search("tests") == [(first, plugin)]

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


class TestPluginMetadata:
    async def test_author_and_updated_parsed(self, market: PluginMarket) -> None:
        plugin = market.find("mailflow-test-plugin")
        assert plugin is not None
        _repo, entry = plugin
        assert entry.author == "tester"
        assert entry.updated == "2026-07-15"


class TestMarketCache:
    def test_versioned_cache_preserves_source_and_skips_bad_entries(self) -> None:
        source = Repository("origin", "https://example.invalid/index")
        raw = serialize_market_cache([(source, MarketPlugin.model_validate(PLUGIN_A))])
        payload = json.loads(raw)
        assert payload["version"] == 1
        assert payload["entries"][0]["repository"] == {
            "name": source.name,
            "url": source.url,
        }
        payload["entries"].extend(
            [
                {"repository": {"name": "broken", "url": ""}, "plugin": {"name": "missing id"}},
                None,
            ]
        )

        loaded = deserialize_market_cache(json.dumps(payload))

        assert len(loaded) == 1
        repository, plugin = loaded[0]
        assert (repository.name, repository.url) == (source.name, source.url)
        assert repository.stale is True
        assert plugin.id == PLUGIN_A["id"]

    def test_legacy_cache_uses_unknown_stale_source_and_skips_bad_items(self) -> None:
        loaded = deserialize_market_cache(json.dumps([PLUGIN_A, {"name": "missing id"}, None]))

        assert len(loaded) == 1
        repository, plugin = loaded[0]
        assert (repository.name, repository.url) == ("cache", "")
        assert repository.stale is True
        assert plugin.id == PLUGIN_A["id"]

    def test_merge_cache_only_for_failed_sources_and_deduplicates(self) -> None:
        fresh_repo = Repository("fresh", "https://fresh.invalid")
        failed_repo = Repository("failed", "https://failed.invalid")
        fresh = MarketPlugin.model_validate(PLUGIN_A)
        cached_failed = MarketPlugin(id="cached-failed", name="Cached failed")
        cached_fresh = MarketPlugin(id="cached-fresh", name="Stale fresh")
        report = MarketFetchReport(
            entries=[(fresh_repo, fresh)],
            failures=[(failed_repo, "offline")],
        )
        merged = merge_market_cache(
            report,
            [
                (failed_repo, cached_failed),
                (fresh_repo, cached_fresh),
                (failed_repo, fresh),
            ],
        )

        assert [plugin.id for _repo, plugin in merged.entries] == [fresh.id, cached_failed.id]
        assert merged.used_cache is True

    def test_invalid_cache_payload_is_empty(self) -> None:
        assert deserialize_market_cache("not json") == []
        assert deserialize_market_cache('{"version":99,"entries":[]}') == []
