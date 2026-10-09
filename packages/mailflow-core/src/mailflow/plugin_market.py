"""Plugin marketplace: browse remote plugin repositories and install plugins.

A repository is a URL serving a root ``index.json`` (``{name, schema,
categories: [{id, path}]}``) plus category folders. Each plugin lives in its
own folder ``<category>/<plugin-id>/plugin.json`` containing the full
metadata including the markdown readme:

.. code-block:: json

    {
      "id": "mailflow-notify-ntfy",
      "name": "ntfy Notifier",
      "description": "Push mail alerts to any ntfy.sh topic",
      "categories": ["notifier"],
      "package": "mailflow-notify-ntfy",
      "source": "git+https://github.com/...",
      "readme": "# ...markdown..."
    }

Adding a plugin means adding exactly one folder, so pull requests never
conflict over a shared index. Installing runs ``uv pip install <source>`` in
the active environment; the new plugin is discovered on the next service
start (entry-point discovery).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, cast
from urllib.error import URLError

from pydantic import BaseModel, Field

logger = logging.getLogger("mailflow.market")

_UV_TIMEOUT = 600.0
"""Seconds before a hung uv install/uninstall is killed instead of blocking
the update loop forever."""
_FETCH_TIMEOUT = 15.0


class MarketPlugin(BaseModel):
    """One entry in a marketplace index."""

    id: str
    name: str = ""
    version: str = ""
    description: str = ""
    categories: list[str] = Field(default_factory=lambda: [])
    package: str = ""
    source: str = ""  # pip spec, git URL or local directory path
    entry_point: str = "mailflow.plugins"
    author: str = ""
    license: str = ""
    homepage: str = ""
    updated: str = ""  # ISO date of the last plugin update (optional)
    readme: str = ""  # markdown long description shown in the detail view
    # locale code -> translated one-line description / markdown readme; the
    # active app language is preferred, falling back to the default fields.
    descriptions: dict[str, str] = Field(default_factory=lambda: {})
    readmes: dict[str, str] = Field(default_factory=lambda: {})

    def description_for(self, language: str) -> str:
        """One-line description for a language, falling back to the default."""
        return self.descriptions.get(language) or self.description

    def readme_for(self, language: str) -> str:
        """Markdown readme for a language, falling back to the default."""
        return self.readmes.get(language) or self.readme


class MarketIndex(BaseModel):
    name: str = ""
    plugins: list[MarketPlugin] = Field(default_factory=lambda: [])


@dataclass(frozen=True)
class Repository:
    name: str
    url: str
    # Cache-loaded repositories are known to be stale until refreshed. This
    # presentation hint is not part of configured repository identity.
    stale: bool = field(default=False, compare=False, repr=False)


@dataclass(frozen=True)
class MarketFetchReport:
    entries: list[tuple[Repository, MarketPlugin]]
    failures: list[tuple[Repository, str]]
    repositories: list[MarketRepositoryStatus] = field(default_factory=lambda: [])
    used_cache: bool = False


@dataclass(frozen=True)
class MarketRepositoryStatus:
    """Result for one configured repository fetch."""

    repository: Repository
    ok: bool
    plugin_count: int = 0
    error: str = ""


MARKET_CACHE_VERSION = 1
_UNKNOWN_CACHE_REPOSITORY = Repository("cache", "", stale=True)


def serialize_market_cache(entries: list[tuple[Repository, MarketPlugin]]) -> str:
    """Serialize marketplace entries with repository provenance and version."""
    payload = {
        "version": MARKET_CACHE_VERSION,
        "entries": [
            {
                "repository": {"name": repository.name, "url": repository.url},
                "plugin": plugin.model_dump(mode="json"),
            }
            for repository, plugin in entries
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def deserialize_market_cache(raw: str) -> list[tuple[Repository, MarketPlugin]]:
    """Load current or legacy cache data, skipping malformed entries.

    Cache entries are stale by definition until a live fetch replaces them.
    The legacy list contained plugins only, so its source is represented by
    the stable ``cache`` sentinel with an empty URL.
    """
    try:
        payload: Any = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []

    if isinstance(payload, list):
        legacy_entries: list[tuple[Repository, MarketPlugin]] = []
        for item in cast(list[Any], payload):
            try:
                legacy_entries.append(
                    (_UNKNOWN_CACHE_REPOSITORY, MarketPlugin.model_validate(item))
                )
            except Exception:
                continue
        return legacy_entries

    if not isinstance(payload, dict):
        return []
    payload_map = cast(dict[str, Any], payload)
    if payload_map.get("version") != MARKET_CACHE_VERSION:
        return []
    entries = payload_map.get("entries")
    if not isinstance(entries, list):
        return []

    cached_entries: list[tuple[Repository, MarketPlugin]] = []
    for raw_item in cast(list[Any], entries):
        if not isinstance(raw_item, dict):
            continue
        item = cast(dict[str, Any], raw_item)
        try:
            plugin = MarketPlugin.model_validate(item.get("plugin"))
        except Exception:
            continue
        source = item.get("repository")
        if isinstance(source, dict):
            source_map = cast(dict[str, Any], source)
            name, url = source_map.get("name"), source_map.get("url")
        else:
            name, url = None, None
        repository = (
            Repository(name, url, stale=True)
            if isinstance(name, str) and isinstance(url, str)
            else _UNKNOWN_CACHE_REPOSITORY
        )
        cached_entries.append((repository, plugin))
    return cached_entries


def merge_market_cache(
    report: MarketFetchReport,
    cached: list[tuple[Repository, MarketPlugin]],
) -> MarketFetchReport:
    """Retain cached entries only for repositories that failed to refresh.

    A successful empty repository is authoritative and must clear old entries;
    cache data is a fallback for transport/index failures, never a second
    source of duplicate marketplace rows.
    """
    entries = list(report.entries)
    seen_ids = {plugin.id for _repository, plugin in entries}
    added = False
    for cached_repository, cached_plugin in cached:
        if cached_plugin.id in seen_ids:
            continue
        failed_source = any(
            repository.name == cached_repository.name and repository.url == cached_repository.url
            for repository, _reason in report.failures
        )
        unknown_source = (
            cached_repository.name == "cache"
            and not cached_repository.url
            and bool(report.failures)
        )
        if not (failed_source or unknown_source):
            continue
        entries.append((cached_repository, cached_plugin))
        seen_ids.add(cached_plugin.id)
        added = True
    return MarketFetchReport(
        entries=entries,
        failures=list(report.failures),
        repositories=list(report.repositories),
        used_cache=report.used_cache or added,
    )


def safe_failure_reason(exc: Exception) -> str:
    """Bound and redact transport errors before exposing them in a report."""
    reason = str(exc).strip() or type(exc).__name__
    reason = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1[redacted]@", reason)
    reason = re.sub(
        r"(?i)([?&](?:[^=&#]*(?:api[_-]?key|access[_-]?token|token|password|secret|authorization|signature))=)[^&#\s]*",
        r"\1[redacted]",
        reason,
    )
    reason = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|token|password|secret|authorization)(\s*[=:]\s*)(?:(?:bearer|basic)\s+)?[^\s,;&]+",
        r"\1\2[redacted]",
        reason,
    )
    reason = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[redacted]", reason)
    reason = "".join(char if char.isprintable() else " " for char in reason)
    return reason[:300] or type(exc).__name__


def _fetch_json(url: str, timeout: float = _FETCH_TIMEOUT) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class PluginMarket:
    """Fetches indexes from configured repositories and installs plugins."""

    def __init__(self, repositories: list[Repository]) -> None:
        self._repositories = list(repositories)

    @staticmethod
    def _join(base: str, *parts: str) -> str:
        return "/".join([base.rstrip("/"), *parts])

    @staticmethod
    def _file_base(url: str) -> str:
        """Base URL for fetching raw files from a repository.

        A ``github.com`` web URL serves HTML pages, not file contents, so
        index.json/plugin.json must come from the matching raw host; the
        Contents API stays responsible only for directory listings.
        """
        match = re.search(r"github\.com/([^/]+)/([^/]+)", url)
        if match is None:
            return url
        branch = "main"
        branch_match = re.search(r"(?:/tree/|@)([^/]+)", url)
        if branch_match:
            branch = branch_match.group(1)
        return f"https://raw.githubusercontent.com/{match.group(1)}/{match.group(2)}/{branch}"

    def _list_plugin_dirs(self, base: str, category_path: str, timeout: float) -> list[str]:
        """Return the plugin directory names inside one category folder."""
        if base.startswith("file://"):
            from pathlib import Path
            from urllib.parse import unquote, urlparse

            raw_path = unquote(urlparse(base).path)
            if re.match(r"^/[A-Za-z]:", raw_path):
                raw_path = raw_path[1:]  # Windows drive: /C:/... -> C:/...
            directory = Path(raw_path) / category_path
            if not directory.is_dir():
                return []
            return sorted(item.name for item in directory.iterdir() if item.is_dir())
        match = re.search(r"github\.com/([^/]+)/([^/]+)", base)
        if match is not None:
            owner, repo = match.group(1), match.group(2)
            branch = "main"
            branch_match = re.search(r"(?:/tree/|@)([^/]+)", base)
            if branch_match:
                branch = branch_match.group(1)
            api_url = (
                f"https://api.github.com/repos/{owner}/{repo}/contents/{category_path}?ref={branch}"
            )
            payload = _fetch_json(api_url, timeout)
            if isinstance(payload, list):
                entries = cast(list[dict[str, Any]], payload)
                return sorted(str(entry["name"]) for entry in entries if entry.get("type") == "dir")
            return []
        # generic HTTP server: per-category manifest fallback
        manifest = _fetch_json(self._join(base, category_path, "INDEX.json"), timeout)
        if not isinstance(manifest, dict):
            return []
        mapping = cast(dict[str, Any], manifest)
        return sorted(str(name) for name in mapping.get("plugins", []))

    def fetch_index(
        self, repository: Repository, timeout: float = _FETCH_TIMEOUT
    ) -> list[MarketPlugin]:
        """Fetch every per-plugin metadata file; a broken file is skipped."""
        root = _fetch_json(self._join(self._file_base(repository.url), "index.json"), timeout)
        if not isinstance(root, dict):
            raise ValueError("marketplace index.json is not a JSON object")
        root_map = cast(dict[str, Any], root)
        categories = root_map.get("categories", [])
        if not isinstance(categories, list):
            raise ValueError("marketplace index.json has no categories list")
        plugins: list[MarketPlugin] = []
        category_list: list[Any] = cast("list[Any]", categories)
        for category in category_list:
            if not isinstance(category, dict):
                continue
            category_map = cast(dict[str, Any], category)
            category_path = str(category_map.get("path", ""))
            if not category_path:
                continue
            file_base = self._file_base(repository.url)
            for plugin_dir in self._list_plugin_dirs(repository.url, category_path, timeout):
                metadata_url = self._join(file_base, category_path, plugin_dir, "plugin.json")
                try:
                    payload = _fetch_json(metadata_url, timeout)
                    # one broken plugin.json must not hide the whole repository
                    plugins.append(MarketPlugin.model_validate(payload))
                except (URLError, ValueError, json.JSONDecodeError) as exc:
                    logger.error(
                        "invalid plugin metadata %s/%s: %s", category_path, plugin_dir, exc
                    )
                    continue
        return plugins

    def list_plugins_report(self, timeout: float = _FETCH_TIMEOUT) -> MarketFetchReport:
        """Fetch configured repositories in order and report isolated failures.

        The first valid metadata for an id wins. This also collapses duplicate
        category references inside one repository, preserving repository priority.
        """
        entries: list[tuple[Repository, MarketPlugin]] = []
        failures: list[tuple[Repository, str]] = []
        repositories: list[MarketRepositoryStatus] = []
        seen_ids: set[str] = set()
        for repository in self._repositories:
            try:
                index = self.fetch_index(repository, timeout)
            except Exception as exc:
                reason = safe_failure_reason(exc)
                logger.error("marketplace %r unreachable: %s", repository.name, reason)
                failures.append((repository, reason))
                repositories.append(
                    MarketRepositoryStatus(repository=repository, ok=False, error=reason)
                )
                continue
            repositories.append(
                MarketRepositoryStatus(repository=repository, ok=True, plugin_count=len(index))
            )
            for plugin in index:
                if plugin.id in seen_ids:
                    logger.warning(
                        "duplicate marketplace plugin id %r in repository %r; keeping first metadata",
                        plugin.id,
                        repository.name,
                    )
                    continue
                seen_ids.add(plugin.id)
                entries.append((repository, plugin))
        return MarketFetchReport(
            entries=entries,
            failures=failures,
            repositories=repositories,
        )

    def list_plugins(
        self, timeout: float = _FETCH_TIMEOUT
    ) -> list[tuple[Repository, MarketPlugin]]:
        """Compatibility convenience returning only deduplicated entries."""
        return self.list_plugins_report(timeout).entries

    def find(
        self, plugin_id: str, timeout: float = _FETCH_TIMEOUT
    ) -> tuple[Repository, MarketPlugin] | None:
        for repository, plugin in self.list_plugins(timeout):
            if plugin.id == plugin_id:
                return repository, plugin
        return None

    def search(
        self,
        query: str,
        category: str = "",
        language: str = "",
        timeout: float = _FETCH_TIMEOUT,
    ) -> list[tuple[Repository, MarketPlugin]]:
        """Filter plugins by name/description (case-insensitive) and category.
        Localized descriptions are matched too when a language is given."""
        haystack = query.strip().lower()
        results: list[tuple[Repository, MarketPlugin]] = []
        for repository, plugin in self.list_plugins(timeout):
            if category and category not in plugin.categories:
                continue
            if haystack:
                blob = f"{plugin.id} {plugin.name} {plugin.description}".lower()
                if language:
                    blob += f" {plugin.description_for(language)}".lower()
                if haystack not in blob:
                    continue
            results.append((repository, plugin))
        return results

    @staticmethod
    def is_installed(plugin_id: str, group: str = "mailflow.plugins", package: str = "") -> bool:
        """True when the plugin id is a registered entry point or its pip
        package distribution is present in the environment."""
        try:
            if any(ep.name == plugin_id for ep in metadata.entry_points().select(group=group)):
                return True
        except Exception:
            pass
        if package:
            try:
                metadata.distribution(package)
                return True
            except metadata.PackageNotFoundError:
                pass
        return False

    async def install(self, plugin: MarketPlugin, *, check: bool = True) -> str:
        """Install one plugin via uv pip; returns installer output."""
        if check and self.is_installed(plugin.id, package=plugin.package):
            return f"{plugin.id} is already installed"
        if not plugin.source:
            raise ValueError(f"plugin {plugin.id!r} has no install source")
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("uv executable not found on PATH; cannot install plugins")
        # --no-deps: the host already provides mailflow-core; plugins may be
        # installed from local directories or unpublished git refs.
        command = [uv, "pip", "install", "--no-deps", plugin.source]
        logger.info("installing plugin %r via %s", plugin.id, " ".join(command))
        result = await asyncio.to_thread(
            subprocess.run,
            command,
            capture_output=True,
            text=True,
            timeout=_UV_TIMEOUT,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"uv pip install failed: {(result.stderr or result.stdout).strip()[:500]}"
            )
        return (result.stdout or result.stderr or "").strip()

    async def uninstall(self, plugin: MarketPlugin) -> str:
        """Uninstall one plugin via uv pip; returns installer output."""
        if not plugin.package:
            raise ValueError(f"plugin {plugin.id!r} has no pip package to uninstall")
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("uv executable not found on PATH; cannot uninstall plugins")
        command = [uv, "pip", "uninstall", "-q", plugin.package]
        logger.info("uninstalling plugin %r via %s", plugin.id, " ".join(command))
        result = await asyncio.to_thread(
            subprocess.run,
            command,
            capture_output=True,
            text=True,
            timeout=_UV_TIMEOUT,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"uv pip uninstall failed: {(result.stderr or result.stdout).strip()[:500]}"
            )
        return (result.stdout or result.stderr or "").strip()


__all__ = [
    "MARKET_CACHE_VERSION",
    "MarketFetchReport",
    "MarketIndex",
    "MarketPlugin",
    "MarketRepositoryStatus",
    "PluginMarket",
    "Repository",
    "deserialize_market_cache",
    "detect_plugin_folders",
    "merge_market_cache",
    "serialize_market_cache",
]


def detect_plugin_folders(root: str | Path) -> list[Path]:
    """Locate plugin folders under ``root``: the root itself when it is a
    plugin (has ``plugin.json``), otherwise its immediate subfolders that
    look like plugins (``plugin.json`` or a pip-installable ``pyproject``)."""
    base = Path(root)
    if (base / "plugin.json").is_file():
        return [base]
    candidates = sorted(p for p in base.iterdir() if p.is_dir())
    with_metadata = [p for p in candidates if (p / "plugin.json").is_file()]
    if with_metadata:
        return with_metadata
    return [p for p in candidates if (p / "pyproject.toml").is_file()]
