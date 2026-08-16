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
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from importlib import metadata
from typing import Any, cast
from urllib.error import URLError

from pydantic import BaseModel, Field

logger = logging.getLogger("mailflow.market")

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

    def _list_plugin_dirs(self, base: str, category_path: str, timeout: float) -> list[str]:
        """Return the plugin directory names inside one category folder."""
        if base.startswith("file://"):
            import re
            from pathlib import Path
            from urllib.parse import unquote, urlparse

            raw_path = unquote(urlparse(base).path)
            if re.match(r"^/[A-Za-z]:", raw_path):
                raw_path = raw_path[1:]  # Windows drive: /C:/... -> C:/...
            directory = Path(raw_path) / category_path
            if not directory.is_dir():
                return []
            return sorted(item.name for item in directory.iterdir() if item.is_dir())
        if "github.com" in base:
            import re

            match = re.search(r"github\.com/([^/]+)/([^/]+)", base)
            if match is None:
                return []
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
        root = _fetch_json(self._join(repository.url, "index.json"), timeout)
        if not isinstance(root, dict):
            raise ValueError("marketplace index.json is not a JSON object")
        root_map = cast(dict[str, Any], root)
        categories = root_map.get("categories", [])
        if not isinstance(categories, list):
            raise ValueError("marketplace index.json has no categories list")
        plugins: list[MarketPlugin] = []
        for category in cast(list[Any], categories):  # type: ignore[redundant-cast]
            if not isinstance(category, dict):
                continue
            category_map = cast(dict[str, Any], category)
            category_path = str(category_map.get("path", ""))
            if not category_path:
                continue
            for plugin_dir in self._list_plugin_dirs(repository.url, category_path, timeout):
                metadata_url = self._join(repository.url, category_path, plugin_dir, "plugin.json")
                try:
                    payload = _fetch_json(metadata_url, timeout)
                except (URLError, ValueError, json.JSONDecodeError) as exc:
                    logger.error(
                        "invalid plugin metadata %s/%s: %s", category_path, plugin_dir, exc
                    )
                    continue
                plugins.append(MarketPlugin.model_validate(payload))
        return plugins

    def list_plugins(
        self, timeout: float = _FETCH_TIMEOUT
    ) -> list[tuple[Repository, MarketPlugin]]:
        """Fetch every repository; a failing repo is logged and skipped."""
        results: list[tuple[Repository, MarketPlugin]] = []
        for repository in self._repositories:
            try:
                index = self.fetch_index(repository, timeout)
            except (URLError, ValueError, json.JSONDecodeError) as exc:
                logger.error("marketplace %r unreachable: %s", repository.name, exc)
                continue
            for plugin in index:
                results.append((repository, plugin))
        return results

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
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"uv pip uninstall failed: {(result.stderr or result.stdout).strip()[:500]}"
            )
        return (result.stdout or result.stderr or "").strip()


__all__ = ["MarketIndex", "MarketPlugin", "PluginMarket", "Repository"]
