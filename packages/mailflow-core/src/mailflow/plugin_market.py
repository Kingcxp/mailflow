"""Plugin marketplace: browse remote plugin indexes and install plugins.

A marketplace repository is a URL serving a ``plugins.json`` index:

.. code-block:: json

    {
      "name": "mailflow-plugins",
      "plugins": [
        {
          "id": "mailflow-notify-webhook",
          "name": "Webhook Notifier",
          "version": "0.1.0",
          "description": "Deliver mail alerts to an HTTP webhook",
          "categories": ["notifier"],
          "package": "mailflow-notify-webhook",
          "source": "https://github.com/acme/mailflow-notify-webhook",
          "entry_point": "mailflow.plugins",
          "author": "acme",
          "license": "MIT"
        }
      ]
    }

Installing runs ``uv pip install <source>`` in the active environment; the new
plugin is discovered on the next service start (entry-point discovery).
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


class MarketIndex(BaseModel):
    name: str = ""
    plugins: list[MarketPlugin] = Field(default_factory=lambda: [])


@dataclass(frozen=True)
class Repository:
    name: str
    url: str


def _fetch_json(url: str, timeout: float = _FETCH_TIMEOUT) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("marketplace index is not a JSON object")
    return cast(dict[str, Any], payload)


class PluginMarket:
    """Fetches indexes from configured repositories and installs plugins."""

    def __init__(self, repositories: list[Repository]) -> None:
        self._repositories = list(repositories)

    def fetch_index(self, repository: Repository, timeout: float = _FETCH_TIMEOUT) -> MarketIndex:
        return MarketIndex.model_validate(_fetch_json(repository.url, timeout))

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
            for plugin in index.plugins:
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
        self, query: str, category: str = "", timeout: float = _FETCH_TIMEOUT
    ) -> list[tuple[Repository, MarketPlugin]]:
        """Filter plugins by name/description (case-insensitive) and category."""
        haystack = query.strip().lower()
        results: list[tuple[Repository, MarketPlugin]] = []
        for repository, plugin in self.list_plugins(timeout):
            if category and category not in plugin.categories:
                continue
            if haystack:
                blob = f"{plugin.id} {plugin.name} {plugin.description}".lower()
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
